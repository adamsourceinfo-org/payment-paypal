from fastapi import APIRouter

from app import db
from app.config import get_settings
from app.paypal import client

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz():
    """db 的 server_user 與 database 由 DB 自己回答 ——
    回音環境變數證明不了任何事。paypal 區塊絕不回 token 本身。"""
    s = get_settings()
    return {
        "service": "payment-paypal",
        "env": s.app_env,
        "version": s.app_version,
        "db": db.db_status(),
        "paypal": {
            "env": s.paypal_env,
            "token": client.token_status(),
            "webhook": "configured" if s.paypal_webhook_id else "unconfigured",
        },
    }
