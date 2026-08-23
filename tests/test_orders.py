import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.auth as auth_mod
import app.paypal.orders as pp
import app.routers.orders as router_mod
import app.store.orders as store_mod

KEY = "test-key"
CALLER = "test-caller"
NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)
ALL_SCOPES = ["orders:read", "orders:write"]


def _row(**kw):
    base = {"id": uuid.uuid4(), "caller_id": CALLER, "reference_id": "ref-1",
            "paypal_order_id": None, "amount": Decimal("10.00"),
            "currency": "USD", "status": "PENDING", "created_at": NOW}
    base.update(kw)
    return base


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.by_ref = {}

    def get_by_reference(self, caller_id, reference_id):
        return self.by_ref.get((caller_id, reference_id))

    def get(self, caller_id, order_id):
        r = self.rows.get(str(order_id))
        return r if r and r["caller_id"] == caller_id else None

    def create(self, caller_id, reference_id, amount, currency, status):
        r = _row(caller_id=caller_id, reference_id=reference_id,
                 amount=amount, currency=currency, status=status)
        self.rows[str(r["id"])] = r
        self.by_ref[(caller_id, reference_id)] = r
        return r

    def attach_paypal_id(self, order_id, paypal_order_id, status):
        r = self.rows[str(order_id)]
        r.update(paypal_order_id=paypal_order_id, status=status)
        return r

    def set_status(self, order_id, status, captured=False):
        r = self.rows[str(order_id)]
        r["status"] = status
        return r

    def list_(self, caller_id, status=None, limit=50, offset=0):
        return [r for r in self.rows.values() if r["caller_id"] == caller_id]


class FakePayPal:
    def __init__(self):
        self.calls = []

    def create_order(self, **kw):
        self.calls.append(kw)
        return {"id": "PP-1", "status": "CREATED",
                "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/x"}]}

    def capture_order(self, pid):
        self.calls.append(("capture", pid))
        return {"id": pid, "status": "COMPLETED"}


@pytest.fixture
def env(monkeypatch):
    store = FakeStore()
    paypal = FakePayPal()
    for name in ("get_by_reference", "get", "create", "attach_paypal_id",
                 "set_status", "list_"):
        monkeypatch.setattr(router_mod.store, name, getattr(store, name))
    monkeypatch.setattr(router_mod.pp, "create_order", paypal.create_order)
    monkeypatch.setattr(router_mod.pp, "capture_order", paypal.capture_order)
    monkeypatch.setattr(auth_mod.api_keys, "lookup", lambda h: {
        "id": "k1", "caller_id": CALLER, "scopes": ALL_SCOPES, "active": True}
        if h == auth_mod.hash_key(KEY) else None)
    monkeypatch.setattr(auth_mod.api_keys, "touch", lambda i: None)

    from app.main import app
    return TestClient(app, raise_server_exceptions=False), store, paypal


H = {"X-API-Key": KEY}


def test_create_order_returns_approve_link(env):
    client, _, _ = env
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "ref-1", "amount": "10.00", "currency": "USD"})
    assert r.status_code == 201, r.text
    assert r.json()["approve_url"] == "https://sandbox.paypal.com/x"
    assert r.json()["paypal_order_id"] == "PP-1"


def test_duplicate_reference_id_is_idempotent(env):
    client, _, paypal = env
    body = {"reference_id": "ref-dup", "amount": "10.00", "currency": "USD"}
    first = client.post("/v1/orders", headers=H, json=body)
    second = client.post("/v1/orders", headers=H, json=body)
    assert first.status_code == 201
    assert second.status_code == 200          # 冪等，不是錯誤
    assert second.json()["id"] == first.json()["id"]
    assert len(paypal.calls) == 1             # 沒有第二次建單


def test_non_usd_rejected_before_calling_paypal(env):
    client, _, paypal = env
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "ref-2", "amount": "300", "currency": "TWD"})
    assert r.status_code == 400
    assert "USD" in r.text
    assert paypal.calls == []                 # 沒有浪費外部呼叫


def test_three_decimal_usd_rejected(env):
    client, _, paypal = env
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "ref-3", "amount": "10.000", "currency": "USD"})
    assert r.status_code == 400
    assert paypal.calls == []


def test_paypal_receives_caller_attribution(env):
    client, _, paypal = env
    client.post("/v1/orders", headers=H, json={
        "reference_id": "ref-4", "amount": "10.00", "currency": "USD"})
    kw = paypal.calls[0]
    assert kw["caller_id"] == CALLER
    assert kw["reference_id"] == "ref-4"


def test_other_callers_order_is_404_not_403(env):
    client, store, _ = env
    other = store.create("someone-else", "x", Decimal("1.00"), "USD", "PENDING")
    r = client.get(f"/v1/orders/{other['id']}", headers=H)
    assert r.status_code == 404


def test_list_only_shows_own_orders(env):
    client, store, _ = env
    store.create("someone-else", "x", Decimal("1.00"), "USD", "PENDING")
    client.post("/v1/orders", headers=H, json={
        "reference_id": "mine", "amount": "10.00", "currency": "USD"})
    items = client.get("/v1/orders", headers=H).json()["items"]
    assert [i["reference_id"] for i in items] == ["mine"]


def test_missing_api_key_is_401(env):
    client, _, _ = env
    assert client.get("/v1/orders").status_code == 401


def test_wrong_api_key_is_401(env):
    client, _, _ = env
    assert client.get("/v1/orders", headers={"X-API-Key": "bad"}).status_code == 401
