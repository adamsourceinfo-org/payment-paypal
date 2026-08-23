"""PayPal HTTP 層：OAuth token 快取、呼叫、錯誤轉譯。

access_token 有效期約 9 小時，只在記憶體快取，到期前 60 秒重換。
不寫進 Secret Manager、不寫進 DB、不寫進日誌 ——
把短期憑證變成需要輪替的長期資產是反模式。
"""
import base64
import logging
import threading
import time

import httpx

from app.config import get_settings
from app.errors import PayPalError

log = logging.getLogger("paypal")

_REFRESH_MARGIN = 60
_token_cache = None          # (token, expires_at)
_token_lock = threading.Lock()


def _http() -> httpx.Client:
    return httpx.Client(timeout=get_settings().paypal_timeout_seconds)


def _fetch_token():
    """回 (access_token, expires_in)。"""
    s = get_settings()
    basic = base64.b64encode(
        f"{s.paypal_client_id}:{s.paypal_client_secret}".encode()).decode()
    with _http() as c:
        r = c.post(
            f"{s.paypal_api_base}/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            content="grant_type=client_credentials",
        )
    if r.status_code != 200:
        # 不記 body —— 換 token 失敗的回應可能夾帶憑證片段
        raise PayPalError(r.status_code, name="OAUTH_FAILED",
                          message="無法取得 access token")
    data = r.json()
    return data["access_token"], int(data.get("expires_in", 32400))


def access_token() -> str:
    global _token_cache
    with _token_lock:
        now = time.time()
        if _token_cache and _token_cache[1] - now > _REFRESH_MARGIN:
            return _token_cache[0]
        token, expires_in = _fetch_token()
        _token_cache = (token, now + expires_in)
        return token


def reset_token_cache() -> None:
    global _token_cache
    _token_cache = None


def call(method: str, path: str, json=None, headers=None) -> dict:
    s = get_settings()
    hdrs = {"Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)

    with _http() as c:
        r = c.request(method, f"{s.paypal_api_base}{path}",
                      json=json, headers=hdrs)

    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:                     # noqa: BLE001
            body = {}
        exc = PayPalError(
            r.status_code,
            name=body.get("name", ""),
            debug_id=body.get("debug_id", ""),
            details=body.get("details", []),
            message=body.get("message", ""),
        )
        # 只記 debug_id 與 name，不記 body（可能含個資）也不記 token
        log.warning("paypal %s %s -> %s %s debug_id=%s",
                    method, path, r.status_code, exc.name, exc.debug_id)
        raise exc

    if not r.content:
        return {}
    return r.json()


def token_status() -> str:
    """健康檢查用。只回 ok / 錯誤型別，絕不回 token 本身。"""
    try:
        access_token()
        return "ok"
    except PayPalError as exc:
        return f"error:{exc.status}"
    except Exception as exc:                  # noqa: BLE001
        return f"error:{type(exc).__name__}"
