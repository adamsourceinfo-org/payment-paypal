"""Webhook 簽章驗證。

這裡有一個非做不可的細節：PayPal 的驗證端點要拿到**我們收到的原始 body**。
如果先用 pydantic/json 解析再重新序列化，位元組就變了（空白、鍵順序、
Unicode 轉義都可能不同），簽章驗證必定失敗。
所以驗證請求的 body 用字串拼接組出來，把原始 bytes 原封不動塞進去。
"""
import json
import logging

from app.config import get_settings
from app.errors import PayPalError
from app.paypal import client

log = logging.getLogger("paypal.webhook")

_HEADER_FIELDS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


def verify(raw_body: bytes, headers) -> bool:
    s = get_settings()
    if not s.paypal_webhook_id:
        return False

    fields = {}
    for key, header in _HEADER_FIELDS.items():
        value = headers.get(header)
        if not value:
            log.warning("webhook 缺少標頭 %s", header)
            return False
        fields[key] = value
    fields["webhook_id"] = s.paypal_webhook_id

    # 前半段用 json.dumps 安全轉義，webhook_event 直接接原始 bytes
    prefix = json.dumps(fields)[:-1]          # 去掉結尾的 }
    payload = (prefix.encode() + b',"webhook_event":' + raw_body + b"}")

    try:
        res = client.call("POST", "/v1/notifications/verify-webhook-signature",
                          content=payload)
    except PayPalError as exc:
        log.warning("驗簽呼叫失敗 status=%s debug_id=%s", exc.status, exc.debug_id)
        return False
    return res.get("verification_status") == "SUCCESS"
