import pytest

import app.config as cfg


class FakeSettings:
    """不是 frozen dataclass，測試才能逐項覆寫。"""
    app_env = "test"
    app_version = "test"
    paypal_env = "sandbox"
    paypal_api_base = "https://api-m.sandbox.paypal.com"
    paypal_client_id = "cid"
    paypal_client_secret = "csecret"
    paypal_webhook_id = "WH-TEST"
    paypal_timeout_seconds = 5.0
    public_base_url = None
    db_pool_max = 3
    db_pool_timeout_seconds = 5.0
    # 推送：測試預設是「已設定」，要測降級的測試自己覆寫成 None／False
    webhook_signing_key = "test-signing-key"
    internal_key = "test-internal-key"
    webhook_timeout_seconds = 10.0
    webhook_enqueue_timeout_seconds = 2.0
    tasks_queue_prefix = "payment-paypal-deliveries"
    tasks_location = "asia-east1"
    push_configured = True
    supported_currencies = frozenset({"USD"})
    log_level = "debug"
    db_instance = "proj:region:inst"
    db_user = "run-runtime@proj.iam"
    db_name = "payment_paypal"
    db_configured = True


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    """塞進 app.config 的單例，而不是 patch get_settings。

    各模組是 `from app.config import get_settings` 綁函式物件的，
    patch 函式只會補到被列舉到的模組；改動單例則所有 importer 都吃得到。
    """
    s = FakeSettings()
    monkeypatch.setattr(cfg, "_settings", s)
    return s
