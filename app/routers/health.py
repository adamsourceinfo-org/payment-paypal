from fastapi import APIRouter, Response

from app import db
from app.config import get_settings
from app.paypal import client
from app.store import deliveries as deliveries_store
from app.store import webhook_endpoints as endpoints_store

router = APIRouter(tags=["health"])


def _push_status() -> dict:
    """推送有沒有設定、有沒有積壓的死信。

    ⚠️ **dead > 0 不讓 /health 回 503。** 503 的意思是「這個服務壞了」，
    而積壓的死信通常代表**caller** 壞了。用 503 表達會讓 ci 的 smoke 因為
    別人的故障而紅燈 —— 那個檢查就從「我們部署成功了嗎」變成
    「所有 caller 今天都好嗎」，而後者不是它的工作。報告它，不要用它決定成敗。
    """
    s = get_settings()
    if not s.push_configured:
        return {"configured": False}
    out = {"configured": True, "queue_prefix": s.tasks_queue_prefix,
           "location": s.tasks_location}
    try:
        out["endpoints_active"] = endpoints_store.count_active()
        out["dead_last_24h"] = deliveries_store.dead_count(24)
    except Exception as exc:                    # noqa: BLE001 — 診斷用
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


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
        # 推送未設定不算不健康：兩把機密缺席時服務仍然完全可用
        # （事件照樣落地，GET /v1/events 照樣拉得到）。
        "push": _push_status(),
    }

    if db_info.get("ok") is not True or token != "ok":
        response.status_code = 503

    return body
