"""PayPal webhook 接收。

路徑是本服務自訂的（PayPal 不規定，只要求 HTTPS 且公網可達）。
這支端點不驗 API key —— PayPal 不會帶 —— 改驗 PayPal 簽章。
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request

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


def _resolve(event_type: str, resource: dict):
    """把事件對應到本地紀錄。

    只查我們自己的表，**不信任 resource.custom_id** —— 同一個 PayPal 商家帳號
    底下還有其他 app，它們的事件也會進來，custom_id 可以是任何值。
    對應不到就回 (None, None, None)，事件仍會落地但 caller_id 為 NULL。
    """
    if event_type.startswith("BILLING.SUBSCRIPTION."):
        row = subs_store.get_by_paypal_id(resource.get("id"))
        if row:
            return row["caller_id"], "subscription", row
    elif event_type == "PAYMENT.SALE.COMPLETED":
        row = subs_store.get_by_paypal_id(resource.get("billing_agreement_id"))
        if row:
            return row["caller_id"], "subscription", row
    elif event_type.startswith("CHECKOUT.ORDER."):
        row = orders_store.get_by_paypal_id(resource.get("id"))
        if row:
            return row["caller_id"], "order", row
    elif event_type.startswith("PAYMENT.CAPTURE."):
        order_id = (resource.get("supplementary_data", {})
                    .get("related_ids", {}).get("order_id"))
        row = orders_store.get_by_paypal_id(order_id) if order_id else None
        if row:
            return row["caller_id"], "order", row
    return None, None, None


@router.post("")
async def receive(request: Request):
    s = get_settings()
    if not s.paypal_webhook_id:
        # 還沒註冊 webhook。不靜靜收下無法驗簽的請求。
        raise HTTPException(status_code=503, detail="webhook 尚未設定")

    raw = await request.body()          # 原始 bytes，驗簽必須用它
    if not verifier.verify(raw, request.headers):
        raise HTTPException(status_code=401, detail="signature verification failed")

    event = json.loads(raw)
    event_id = event.get("id")
    event_type = event.get("event_type", "")
    resource = event.get("resource") or {}

    caller_id, kind, row = _resolve(event_type, resource)
    subject_id = str(row["id"]) if row else None

    new_id = events_store.record(event_id, event_type, caller_id, kind,
                                 subject_id, raw.decode())
    if new_id is None:
        # PayPal 重送 —— 冪等，什麼都不做
        return {"status": "duplicate"}

    if kind == "subscription" and event_type in _SUB_STATUS:
        subs_store.set_status(row["id"], _SUB_STATUS[event_type])
    elif kind == "order" and event_type in _ORDER_STATUS:
        status, captured = _ORDER_STATUS[event_type]
        orders_store.set_status(row["id"], status, captured=captured)
    elif caller_id is None:
        log.info("事件 %s 對應不到 caller，以 caller_id=NULL 落地", event_type)

    return {"status": "ok", "event_id": new_id}
