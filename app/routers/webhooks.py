"""PayPal webhook 接收。

路徑是本服務自訂的（PayPal 不規定，只要求 HTTPS 且公網可達）。
這支端點不驗 API key —— PayPal 不會帶 —— 改驗 PayPal 簽章。

## ⚠️ 併發模型：只有最外層是 async

`receive()` 是**全服務唯一**的 `async def` handler，其餘一律是同步 `def`
（FastAPI 會把同步 handler 丟進 threadpool，沒事）。而這一支裡面有三件
會擋很久的事：

- `verifier.verify()` 打 PayPal 的驗簽 API（一次對外 HTTP）
- pg8000 是**同步** driver，落地事件、更新狀態都是同步 DB

同步呼叫寫在 `async def` 裡會卡住**整個事件迴圈** —— 那個實例上所有請求
跟著排隊，包括 caller 正在同步查詢的 `GET /v1/orders/{id}`。行銷活動當天，
一筆 webhook 就是幾百毫秒的全實例停擺。

所以規則是：**最外層只 `await request.body()`，其餘全部丟 threadpool。**
不要在這個模組裡引入 async DB 或 async httpx —— 混用正是這一類 bug 的來源。
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app import db
from app.config import get_settings
from app.paypal import webhooks as verifier
from app.store import events as events_store
from app.store import orders as orders_store
from app.store import subscriptions as subs_store

log = logging.getLogger("webhook")

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

# 訂閱狀態的事件對應。BILLING.SUBSCRIPTION.PAYMENT.FAILED 刻意不改狀態 ——
# 扣款失敗不等於訂閱結束，PayPal 會依 payment_failure_threshold 重試。
_SUB_STATUS = {
    "BILLING.SUBSCRIPTION.ACTIVATED": "ACTIVE",
    "BILLING.SUBSCRIPTION.CANCELLED": "CANCELLED",
    "BILLING.SUBSCRIPTION.SUSPENDED": "SUSPENDED",
    "BILLING.SUBSCRIPTION.EXPIRED": "EXPIRED",
    "PAYMENT.SALE.COMPLETED": "ACTIVE",       # 每月扣款成功
}
_ORDER_STATUS = {
    "CHECKOUT.ORDER.APPROVED": ("APPROVED", False),
    "PAYMENT.CAPTURE.COMPLETED": ("COMPLETED", True),
    "PAYMENT.CAPTURE.DENIED": ("DENIED", False),
    "PAYMENT.CAPTURE.REFUNDED": ("REFUNDED", False),
}


def _resolve(event_type: str, resource: dict, tx=None):
    """把事件對應到本地紀錄。

    只查我們自己的表，**不信任 resource.custom_id** —— 同一個 PayPal 商家帳號
    底下還有其他 app，它們的事件也會進來，custom_id 可以是任何值。
    對應不到就回 (None, None, None)，事件仍會落地但 caller_id 為 NULL。
    """
    if event_type.startswith("BILLING.SUBSCRIPTION."):
        row = subs_store.get_by_paypal_id(resource.get("id"), tx=tx)
        if row:
            return row["caller_id"], "subscription", row
    elif event_type == "PAYMENT.SALE.COMPLETED":
        row = subs_store.get_by_paypal_id(
            resource.get("billing_agreement_id"), tx=tx)
        if row:
            return row["caller_id"], "subscription", row
    elif event_type.startswith("CHECKOUT.ORDER."):
        row = orders_store.get_by_paypal_id(resource.get("id"), tx=tx)
        if row:
            return row["caller_id"], "order", row
    elif event_type.startswith("PAYMENT.CAPTURE."):
        order_id = (resource.get("supplementary_data", {})
                    .get("related_ids", {}).get("order_id"))
        row = orders_store.get_by_paypal_id(order_id, tx=tx) if order_id else None
        if row:
            return row["caller_id"], "order", row
    return None, None, None


@router.post("")
async def receive(request: Request):
    """最外層只做兩件事：讀原始 bytes、把工作丟進 threadpool。

    ⚠️ 這個函式裡**不可以**出現任何同步 I/O —— 見模組開頭的說明。
    """
    raw = await request.body()          # 原始 bytes，驗簽必須用它
    return await run_in_threadpool(_receive, raw, request.headers)


def _receive(raw: bytes, headers):
    """同步，可以自由用 db 與對外 HTTP。"""
    s = get_settings()
    if not s.paypal_webhook_id:
        # 還沒註冊 webhook。不靜靜收下無法驗簽的請求。
        raise HTTPException(status_code=503, detail="webhook 尚未設定")

    if not verifier.verify(raw, headers):
        raise HTTPException(status_code=401, detail="signature verification failed")

    event = json.loads(raw)
    event_id = event.get("id")
    event_type = event.get("event_type", "")
    resource = event.get("resource") or {}

    # ⚠️ 「落地事件」與「更新本地狀態」必須在**同一個**交易裡。
    # 分成兩次 commit 的話，中間掛掉（實例回收、OOM、逾時）時 PayPal 會重送，
    # 但重送會被 paypal_event_id 的唯一鍵擋掉 → record() 回 None → 早退 →
    # **狀態更新永遠不會執行**。去重鍵一邊做著它該做的事，
    # 一邊堵死了唯一的復原路徑，而突發正是它發作的時候。
    with db.transaction() as tx:
        caller_id, kind, row = _resolve(event_type, resource, tx=tx)
        subject_id = str(row["id"]) if row else None

        new_id = events_store.record(event_id, event_type, caller_id, kind,
                                     subject_id, raw.decode(), tx=tx)
        if new_id is None:
            # PayPal 重送 —— 冪等，什麼都不做
            return {"status": "duplicate"}
        if kind == "subscription" and event_type in _SUB_STATUS:
            subs_store.set_status(row["id"], _SUB_STATUS[event_type], tx=tx)
        elif kind == "order" and event_type in _ORDER_STATUS:
            status, captured = _ORDER_STATUS[event_type]
            orders_store.set_status(row["id"], status, captured=captured, tx=tx)
        elif caller_id is None:
            log.info("事件 %s 對應不到 caller，以 caller_id=NULL 落地", event_type)

    return {"status": "ok", "event_id": new_id}
