import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.auth as auth_mod
import app.routers.plans as plans_router
import app.routers.subscriptions as subs_router

KEY = "k"
CALLER = "c1"
SCOPES = ["plans:read", "plans:write", "subscriptions:read", "subscriptions:write"]


def _plan(caller=CALLER, **kw):
    base = {"id": uuid.uuid4(), "caller_id": caller, "paypal_product_id": "PROD-1",
            "paypal_plan_id": "P-1", "name": "Basic", "amount": Decimal("9.99"),
            "currency": "USD", "interval_unit": "MONTH", "interval_count": 1,
            "status": "ACTIVE", "created_at": None}
    base.update(kw)
    return base


@pytest.fixture
def env(monkeypatch):
    plans, subs, pp_calls = {}, {}, []

    def plans_get(caller_id, plan_id):
        r = plans.get(str(plan_id))
        return r if r and r["caller_id"] == caller_id else None

    def plans_create(caller_id, prod, plan_id, name, amount, currency, ic):
        r = _plan(caller=caller_id, paypal_product_id=prod, paypal_plan_id=plan_id,
                  name=name, amount=amount, currency=currency, interval_count=ic)
        plans[str(r["id"])] = r
        return r

    monkeypatch.setattr(plans_router.store, "get", plans_get)
    monkeypatch.setattr(plans_router.store, "create", plans_create)
    monkeypatch.setattr(plans_router.store, "list_",
                        lambda c, limit=50, offset=0:
                        [p for p in plans.values() if p["caller_id"] == c])
    monkeypatch.setattr(plans_router.pp, "create_product",
                        lambda n, d=None: pp_calls.append(("product", n)) or
                        {"id": "PROD-X"})
    monkeypatch.setattr(plans_router.pp, "create_plan",
                        lambda **kw: pp_calls.append(("plan", kw)) or {"id": "P-X"})

    monkeypatch.setattr(subs_router.plans_store, "get", plans_get)
    monkeypatch.setattr(subs_router.store, "get_by_reference",
                        lambda c, r: subs.get((c, r)))

    def subs_create(caller_id, plan_id, reference_id, status):
        r = {"id": uuid.uuid4(), "caller_id": caller_id, "plan_id": plan_id,
             "reference_id": reference_id, "paypal_subscription_id": None,
             "status": status, "current_period_end": None, "created_at": None}
        subs[(caller_id, reference_id)] = r
        return r

    monkeypatch.setattr(subs_router.store, "create", subs_create)
    monkeypatch.setattr(subs_router.store, "attach_paypal_id",
                        lambda sid, pid, st: {"id": sid, "caller_id": CALLER,
                                              "plan_id": "p", "reference_id": "r",
                                              "paypal_subscription_id": pid,
                                              "status": st,
                                              "current_period_end": None,
                                              "created_at": None})
    monkeypatch.setattr(subs_router.pp, "create_subscription",
                        lambda **kw: pp_calls.append(("sub", kw)) or
                        {"id": "I-X", "status": "APPROVAL_PENDING",
                         "links": [{"rel": "approve", "href": "https://x/approve"}]})

    monkeypatch.setattr(auth_mod.api_keys, "lookup", lambda h: {
        "id": "k1", "caller_id": CALLER, "scopes": SCOPES, "active": True}
        if h == auth_mod.hash_key(KEY) else None)
    monkeypatch.setattr(auth_mod.api_keys, "touch", lambda i: None)

    from app.main import app
    return TestClient(app, raise_server_exceptions=False), plans, pp_calls


H = {"X-API-Key": KEY}


def test_create_plan_creates_product_then_plan(env):
    client, _, calls = env
    r = client.post("/v1/plans", headers=H, json={
        "name": "Basic 月費", "amount": "9.99", "currency": "USD"})
    assert r.status_code == 201, r.text
    assert [c[0] for c in calls] == ["product", "plan"]


def test_plan_currency_must_be_supported(env):
    client, _, calls = env
    r = client.post("/v1/plans", headers=H, json={
        "name": "x", "amount": "300", "currency": "TWD"})
    assert r.status_code == 400
    assert calls == []                      # 沒打 PayPal


def test_plans_are_caller_scoped(env):
    client, plans, _ = env
    theirs = _plan(caller="other")
    plans[str(theirs["id"])] = theirs
    client.post("/v1/plans", headers=H, json={
        "name": "mine", "amount": "9.99", "currency": "USD"})
    names = [p["name"] for p in client.get("/v1/plans", headers=H).json()["items"]]
    assert names == ["mine"]


def test_other_callers_plan_is_404(env):
    client, plans, _ = env
    theirs = _plan(caller="other")
    plans[str(theirs["id"])] = theirs
    assert client.get(f"/v1/plans/{theirs['id']}", headers=H).status_code == 404


def test_create_subscription_returns_approve_link(env):
    client, plans, _ = env
    mine = _plan()
    plans[str(mine["id"])] = mine
    r = client.post("/v1/subscriptions", headers=H, json={
        "reference_id": "sub-1", "plan_id": str(mine["id"])})
    assert r.status_code == 201, r.text
    assert r.json()["approve_url"] == "https://x/approve"
    assert r.json()["status"] == "APPROVAL_PENDING"


def test_subscription_reference_id_is_idempotent(env):
    client, plans, calls = env
    mine = _plan()
    plans[str(mine["id"])] = mine
    body = {"reference_id": "sub-dup", "plan_id": str(mine["id"])}
    a = client.post("/v1/subscriptions", headers=H, json=body)
    b = client.post("/v1/subscriptions", headers=H, json=body)
    assert a.status_code == 201 and b.status_code == 200
    assert len([c for c in calls if c[0] == "sub"]) == 1


def test_cannot_subscribe_to_another_callers_plan(env):
    client, plans, calls = env
    theirs = _plan(caller="other")
    plans[str(theirs["id"])] = theirs
    r = client.post("/v1/subscriptions", headers=H, json={
        "reference_id": "sub-x", "plan_id": str(theirs["id"])})
    assert r.status_code == 404
    assert calls == []


def test_subscription_sends_custom_id_to_paypal(env):
    client, plans, calls = env
    mine = _plan()
    plans[str(mine["id"])] = mine
    client.post("/v1/subscriptions", headers=H, json={
        "reference_id": "sub-2", "plan_id": str(mine["id"])})
    sub_call = [c for c in calls if c[0] == "sub"][0][1]
    assert sub_call["custom_id"] == CALLER
    assert sub_call["paypal_plan_id"] == "P-1"
