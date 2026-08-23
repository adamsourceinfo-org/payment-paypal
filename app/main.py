import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.config import get_settings

log = logging.getLogger("payment-paypal")


class _RedactFilter(logging.Filter):
    """最後一道防線：機密永遠不該進日誌，但人會犯錯。"""

    def filter(self, record):
        s = get_settings()
        secrets = [v for v in (s.paypal_client_secret,) if v]
        try:
            msg = record.getMessage()
        except Exception:                       # noqa: BLE001
            return True
        for sec in secrets:
            if sec and sec in msg:
                record.msg = msg.replace(sec, "***redacted***")
                record.args = ()
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO))
    logging.getLogger().addFilter(_RedactFilter())
    if s.db_configured:
        try:
            applied = db.run_migrations()
            log.info("migration 套用 %s", applied or "（無新項目）")
        except Exception as exc:                # noqa: BLE001
            # 不讓 migration 失敗擋住啟動，健康檢查會把 db 標成 not ok
            log.error("migration 失敗：%s: %s", type(exc).__name__, exc)
    else:
        log.warning("DB 未設定，跳過 migration")
    yield


app = FastAPI(title="payment-paypal", version="1", lifespan=lifespan)


def _mount():
    from app.routers import health, orders
    app.include_router(health.router)
    app.include_router(orders.router)


_mount()
