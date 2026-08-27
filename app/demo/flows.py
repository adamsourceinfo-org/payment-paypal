"""demo 的動作。這一層不碰 FastAPI 的 Request/Response。

## 為什麼直接呼叫 router 函式

⚠️ **不要為了 demo 再實作一次建單邏輯。** 金額驗證、冪等（同 reference_id
回原本那筆）、狀態機、404 語意全部沿用 `/v1/*` 那一份。手抄一份的話，
那份平常不會被真流量執行 —— 而那是最糟的一種程式碼。
（同樣的理由見 app/event_view.py 的說明。）

## 身分

`caller_id = "demo"`。每張業務表都有 caller_id，所以 demo 的訂單、訂閱、
事件天生跟真 caller 隔離，而 `GET /v1/events` 的游標本來就不匹配別人的。

⚠️ scope 在這裡其實不會被檢查（檢查發生在 `require()` 這個 dependency 裡，
不在函式本體）。還是把完整的 scope 列出來，因為這個物件代表的是
「一個有這些權限的 caller」—— 寫成空集合會讓讀的人以為 scope 不重要。
"""
import logging
import uuid

from fastapi import HTTPException

from app.auth import Caller
from app.models import OrderCreate, PlanCreate, SubscriptionCreate
from app.routers import orders as orders_router
from app.routers import plans as plans_router
from app.routers import subscriptions as subs_router
from app.store import orders as orders_store
from app.store import plans as plans_store

log = logging.getLogger("demo")

DEMO_CALLER_ID = "demo"

DEMO_CALLER = Caller(
    caller_id=DEMO_CALLER_ID,
    scopes=frozenset({
        "orders:read", "orders:write",
        "plans:read", "plans:write",
        "subscriptions:read", "subscriptions:write",
        "events:read",
        "webhooks:read", "webhooks:write",
    }),
)


# demo 的金額固定用 USD —— 帳號只支援這一種（見 SUPPORTED_CURRENCIES）。
DEMO_CURRENCY = "USD"


def _reference() -> str:
    """每次按下去都是一筆新的單。

    ⚠️ 刻意不重用 reference_id：`POST /v1/orders` 對同一個 reference_id 是
    冪等的（回原本那筆），重用的話第二次按下去會拿到第一筆的 approve_url，
    看起來像「按了沒反應」。
    """
    return f"demo-{uuid.uuid4().hex[:12]}"


def start_order(amount: str, base_url: str) -> dict:
    """建一筆單，回 approve_url 讓前端跳過去。

    ⚠️ 導回網址帶的是**我們自己產生的 reference_id**，不是 PayPal 的 token。
    我們建單前就知道它，放進網址是零成本；靠 token 反查要多一次查詢，
    而且訂單與訂閱兩條路 PayPal 帶回來的參數名不一樣（token / subscription_id），
    統一不了。
    """
    ref = _reference()
    body = OrderCreate(
        reference_id=ref,
        amount=amount,
        currency=DEMO_CURRENCY,
        description="payment-paypal demo 單筆付款",
        return_url=f"{base_url}/demo/return/order/{ref}",
        cancel_url=f"{base_url}/demo/cancel/order/{ref}",
    )
    out = orders_router.create_order(body, caller=DEMO_CALLER)
    return {"reference_id": ref, "approve_url": out.get("approve_url"),
            "order": out}


def finish_order(reference_id: str) -> str:
    """使用者從 PayPal 導回來之後 capture。回一個給網址用的結果字串。

    ⚠️ 這裡吞掉例外並回字串，不讓它變成 500 —— 使用者是**在瀏覽器裡**，
    給他一個 stack trace 沒有任何意義。真正的錯誤進 log。
    """
    row = orders_store.get_by_reference(DEMO_CALLER_ID, reference_id)
    if not row:
        return "missing"
    try:
        orders_router.capture(str(row["id"]), caller=DEMO_CALLER)
    except HTTPException as exc:
        log.error("demo capture 失敗 ref=%s：%s", reference_id, exc.detail)
        return "error"
    except Exception as exc:                    # noqa: BLE001
        log.error("demo capture 爆了 ref=%s：%s: %s",
                  reference_id, type(exc).__name__, exc)
        return "error"
    return "paid"


DEMO_PLAN_NAME = "payment-paypal demo 月訂閱"
DEMO_PLAN_AMOUNT = "10.00"


def ensure_plan() -> dict:
    """回 demo 用的方案，沒有就建一個。

    ⚠️ 一定要重用。建方案在 PayPal 那邊是 product + plan 兩個永久物件，
    每按一次訂閱就多一組的話，後台很快就被塞滿而且分不出哪個在用。
    用名字比對就夠了 —— 這是 demo，不需要一個 `is_demo` 欄位。
    """
    for row in plans_store.list_(DEMO_CALLER_ID, limit=200):
        if row["name"] == DEMO_PLAN_NAME and row["status"] == "ACTIVE":
            return {**row, "id": str(row["id"])}
    return plans_router.create_plan(
        PlanCreate(name=DEMO_PLAN_NAME, amount=DEMO_PLAN_AMOUNT,
                   currency=DEMO_CURRENCY, interval_count=1,
                   description="payment-paypal 模擬頁用的月訂閱方案"),
        caller=DEMO_CALLER)


def start_subscription(base_url: str) -> dict:
    """建一筆訂閱，回 approve_url。

    ⚠️ 訂閱**沒有 capture**。使用者在 PayPal 按下訂閱之後，本地狀態要等
    webhook `BILLING.SUBSCRIPTION.ACTIVATED` 才會變成 ACTIVE ——
    所以導回頁只能說「已送出」，真正的確認由推送那條路帶回來。
    """
    plan = ensure_plan()
    ref = _reference()
    body = SubscriptionCreate(
        reference_id=ref,
        plan_id=str(plan["id"]),
        return_url=f"{base_url}/demo/return/subscription/{ref}",
        cancel_url=f"{base_url}/demo/cancel/subscription/{ref}",
    )
    out = subs_router.create_subscription(body, caller=DEMO_CALLER)
    return {"reference_id": ref, "approve_url": out.get("approve_url"),
            "subscription": out}
