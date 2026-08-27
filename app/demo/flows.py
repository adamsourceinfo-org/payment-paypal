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
from app.models import OrderCreate
from app.routers import orders as orders_router
from app.store import orders as orders_store

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
