import httpx
import pytest

import app.paypal.client as pc
from app.errors import PayPalError


def _mock_http(monkeypatch, handler):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(pc, "_http", factory)


def test_token_cached_within_validity(monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return "t%d" % len(calls), 32400

    monkeypatch.setattr(pc, "_fetch_token", fake)
    pc.reset_token_cache()
    assert pc.access_token() == "t1"
    assert pc.access_token() == "t1"
    assert len(calls) == 1


def test_token_refetched_near_expiry(monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return "t%d" % len(calls), 30       # < 60 秒門檻

    monkeypatch.setattr(pc, "_fetch_token", fake)
    pc.reset_token_cache()
    assert pc.access_token() == "t1"
    assert pc.access_token() == "t2"


def test_fetch_token_uses_basic_auth_and_derived_base(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "abc", "expires_in": 32400})

    _mock_http(monkeypatch, handler)
    pc.reset_token_cache()
    assert pc.access_token() == "abc"
    assert seen["url"] == "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    assert seen["auth"].startswith("Basic ")
    assert seen["body"] == "grant_type=client_credentials"


def test_call_raises_paypal_error_with_debug_id(monkeypatch):
    monkeypatch.setattr(pc, "_fetch_token", lambda: ("t", 32400))
    pc.reset_token_cache()
    _mock_http(monkeypatch, lambda r: httpx.Response(422, json={
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "d123",
        "details": [{"issue": "DUPLICATE_INVOICE_ID"}]}))
    with pytest.raises(PayPalError) as e:
        pc.call("POST", "/v2/checkout/orders", json={})
    assert e.value.status == 422
    assert e.value.debug_id == "d123"
    assert e.value.issues == ["DUPLICATE_INVOICE_ID"]


def test_call_sends_bearer_token(monkeypatch):
    monkeypatch.setattr(pc, "_fetch_token", lambda: ("tok-xyz", 32400))
    pc.reset_token_cache()
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"ok": True})

    _mock_http(monkeypatch, handler)
    assert pc.call("GET", "/v2/checkout/orders/1") == {"ok": True}
    assert seen["auth"] == "Bearer tok-xyz"


def test_token_status_never_returns_token(monkeypatch):
    monkeypatch.setattr(pc, "_fetch_token", lambda: ("super-secret-token", 32400))
    pc.reset_token_cache()
    assert pc.token_status() == "ok"


def test_token_status_reports_error(monkeypatch):
    def boom():
        raise PayPalError(401, name="OAUTH_FAILED")
    monkeypatch.setattr(pc, "_fetch_token", boom)
    pc.reset_token_cache()
    assert pc.token_status() == "error:401"
