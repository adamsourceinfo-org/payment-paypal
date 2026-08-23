from fastapi import APIRouter, Response

from app import db
from app.config import get_settings
from app.paypal import client

router = APIRouter(tags=["health"])


# 路徑刻意不是 /healthz —— Google Frontend 在 *.run.app 上會攔截 /healthz
# 自己回 404，請求根本不會進到容器（實測：請求日誌裡完全沒有這條路徑的紀錄，
# 而 /、/docs、/v1/* 都有）。
@router.get("/health")
def health(response: Response):
    """db 的 server_user 與 database 由 DB 自己回答 ——
    回音環境變數證明不了任何事。paypal 區塊絕不回 token 本身。

    **壞掉時要回 503。** ci 的 smoke 只看狀態碼（200-399 就算過），
    完全不看 body。回 200 但內容寫著「db 掛了」對 CI 來說是綠燈，
    那這個健康檢查就什麼都沒證明。

    Cloud Run 的 startup probe 是 TCP，沒有 liveness probe，
    所以 503 不會讓實例被回收 —— 只會讓部署紅燈，正是要的效果。
    """
    s = get_settings()
    db_info = db.db_status()
    token = client.token_status()

    body = {
        "service": "payment-paypal",
        "env": s.app_env,
        "version": s.app_version,
        "db": db_info,
        "paypal": {
            "env": s.paypal_env,
            "token": token,
            # webhook 未設定不算不健康：那是雞生蛋的既定狀態
            # （要先部署拿到 URL 才註冊得了 webhook），第一次部署必須能綠燈。
            "webhook": "configured" if s.paypal_webhook_id else "unconfigured",
        },
    }

    if db_info.get("ok") is not True or token != "ok":
        response.status_code = 503

    return body
