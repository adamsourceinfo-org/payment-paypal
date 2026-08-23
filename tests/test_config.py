import pytest
from app.config import load_settings, PAYPAL_API_BASE

BASE = {
    "PAYPAL_ENV": "sandbox",
    "PAYPAL_CLIENT_ID": "cid",
    "PAYPAL_CLIENT_SECRET": "csecret",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("PAYPAL_ENV", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET",
              "PAYPAL_WEBHOOK_ID", "SUPPORTED_CURRENCIES"):
        monkeypatch.delenv(k, raising=False)


def test_loads_required(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.paypal_env == "sandbox"
    assert s.paypal_client_id == "cid"
    assert s.supported_currencies == frozenset({"USD"})
    assert s.paypal_webhook_id is None


def test_missing_required_raises(monkeypatch):
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")
    with pytest.raises(RuntimeError) as e:
        load_settings()
    assert "PAYPAL_CLIENT_ID" in str(e.value)


def test_bad_paypal_env_raises(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PAYPAL_ENV", "production")
    with pytest.raises(RuntimeError):
        load_settings()


def test_api_base_is_derived():
    assert PAYPAL_API_BASE["sandbox"] == "https://api-m.sandbox.paypal.com"
    assert PAYPAL_API_BASE["live"] == "https://api-m.paypal.com"


def test_paypal_api_base_property(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    assert load_settings().paypal_api_base == "https://api-m.sandbox.paypal.com"


def test_currencies_parsed(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SUPPORTED_CURRENCIES", "USD, eur")
    assert load_settings().supported_currencies == frozenset({"USD", "EUR"})
