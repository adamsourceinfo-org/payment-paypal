"""所有環境變數在這裡讀一次、驗一次。這是唯一碰 os.environ 的模組。

缺少必要變數就啟動失敗 —— Cloud Run 起不來、CI 的 smoke 紅燈、當場知道，
比上線三週後第一筆爭議時才發現好。
"""
import os
from dataclasses import dataclass
from typing import Optional

# base URL 由 PAYPAL_ENV 推導，不做成設定。可設定就有設錯的餘地，
# 而「prod 指到 sandbox」的代價是以為在收錢但沒有。
PAYPAL_API_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_version: str
    paypal_env: str
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: Optional[str]
    paypal_timeout_seconds: float
    public_base_url: Optional[str]
    db_pool_max: int
    db_pool_timeout_seconds: float
    supported_currencies: frozenset
    log_level: str
    db_instance: Optional[str]
    db_user: Optional[str]
    db_name: Optional[str]

    @property
    def paypal_api_base(self) -> str:
        return PAYPAL_API_BASE[self.paypal_env]

    @property
    def db_configured(self) -> bool:
        return bool(self.db_instance and self.db_user and self.db_name)


def _required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"缺少必要環境變數 {name}")
    return v


def _optional(name: str):
    """可以缺席的機密。**一定要 strip。**

    ⚠️ Secret Manager 存的是位元組，而最自然的建立方式
    （`python3 -c 'print(...)' | gcloud secrets create --data-file=-`）
    會把**換行也存進去**。Cloud Run 原樣注入，於是值變成 "abc\n"。

    症狀依用途而異，而且都很難查：
    - `PAYPAL_WEBHOOK_ID` → PayPal 驗簽永遠回 FAILURE

    `_required()` 本來就 strip，所以 client secret 一直沒事 ——
    這幾個可選的必須跟上，否則同一個 repo 裡兩種行為。
    """
    return (os.environ.get(name) or "").strip() or None


def load_settings() -> Settings:
    paypal_env = _required("PAYPAL_ENV")
    if paypal_env not in PAYPAL_API_BASE:
        raise RuntimeError(
            f"PAYPAL_ENV 只能是 {sorted(PAYPAL_API_BASE)}，收到 {paypal_env!r}")

    currencies = frozenset(
        c.strip().upper()
        for c in os.environ.get("SUPPORTED_CURRENCIES", "USD").split(",")
        if c.strip()
    )
    if not currencies:
        raise RuntimeError("SUPPORTED_CURRENCIES 不能是空的")

    return Settings(
        app_env=os.environ.get("APP_ENV", "unknown"),
        app_version=os.environ.get("APP_VERSION", "(dev build)"),
        paypal_env=paypal_env,
        paypal_client_id=_required("PAYPAL_CLIENT_ID"),
        paypal_client_secret=_required("PAYPAL_CLIENT_SECRET"),
        # 允許缺席的其中一個：第一次部署時還沒有 Cloud Run URL，
        # 就還沒辦法去 PayPal 註冊 webhook，就還拿不到這個 id。
        paypal_webhook_id=_optional("PAYPAL_WEBHOOK_ID"),
        paypal_timeout_seconds=float(os.environ.get("PAYPAL_TIMEOUT_SECONDS", "10")),
        # 沒設就由請求自身的 scheme+host 推導 —— 第一次部署時還不知道自己的網址。
        public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/") or None,
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "3")),
        # 借不到連線就等這麼久，然後 PoolExhausted → 503。
        # 不做成無限等：見 app/db.py 的 PoolExhausted。
        db_pool_timeout_seconds=float(
            os.environ.get("DB_POOL_TIMEOUT_SECONDS", "5")),
        supported_currencies=currencies,
        log_level=os.environ.get("LOG_LEVEL", "info"),
        # 這三個由 CI 依部署目標推導注入，寫進 .cicd/env.* 會被 verify 擋下
        db_instance=os.environ.get("INSTANCE_CONNECTION_NAME") or None,
        db_user=os.environ.get("DB_USER") or None,
        db_name=os.environ.get("DB_NAME") or None,
    )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
