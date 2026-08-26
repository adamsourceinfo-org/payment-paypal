"""服務自己的對外網址。

推送要把 Cloud Tasks 指回服務自己的 `/internal/deliveries/{id}`，而那必須是
絕對網址 —— 第一次部署時我們還不知道自己的網址，這是個雞生蛋。
解法是**預設由請求自身推導**：Cloud Run 會把服務網域放進 Host 標頭，
`X-Forwarded-Proto` 是 https。`PUBLIC_BASE_URL` 只在要換自訂網域時覆蓋。

這樣就不需要「先部署一次拿網址、再填設定、再部署一次」。

⚠️ 檔名是 urls.py 而 app/webhooks/targets.py 才是**caller 端點**的網址驗證 ——
兩者無關：這裡推導的是我們自己的網址，那裡驗的是別人給的網址。
"""
from fastapi import Request

from app.config import get_settings


def base_url(request: Request) -> str:
    s = get_settings()
    if s.public_base_url:
        return s.public_base_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"
