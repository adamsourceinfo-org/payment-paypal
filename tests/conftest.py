import pytest

import app.config as cfg


class FakeSettings:
    app_env = "test"
    app_version = "test"
    paypal_env = "sandbox"
    paypal_api_base = "https://api-m.sandbox.paypal.com"
    paypal_client_id = "cid"
    paypal_client_secret = "csecret"
    paypal_webhook_id = "WH-TEST"
    paypal_timeout_seconds = 5.0
    db_pool_max = 3
    supported_currencies = frozenset({"USD"})
    log_level = "debug"
    db_instance = "proj:region:inst"
    db_user = "run-runtime@proj.iam"
    db_name = "payment_paypal"
    db_configured = True


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    s = FakeSettings()
    monkeypatch.setattr(cfg, "get_settings", lambda: s)
    for mod in ("app.money", "app.db", "app.paypal.client",
                "app.paypal.webhooks"):
        try:
            m = __import__(mod, fromlist=["get_settings"])
        except ImportError:
            continue
        if hasattr(m, "get_settings"):
            monkeypatch.setattr(m, "get_settings", lambda: s)
    return s
