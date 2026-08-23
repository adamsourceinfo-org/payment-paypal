import json
import uuid

import pytest
from fastapi.testclient import TestClient

import app.routers.webhooks as wh


class FakeSubs:
    def __init__(self):
        self.rows = {}
        self.status_calls = []

    def add(self, paypal_id, caller="c1"):
        r = {"id": uuid.uuid4(), "caller_id": caller,
             "paypal_subscription_id": paypal_id, "status": "APPROVAL_PENDING"}
        self.rows[paypal_id] = r
        return r

    def get_by_paypal_id(self, pid):
        return self.rows.get(pid)

    def set_status(self, sid, status, period_end=None):
        self.status_calls.append((sid, status))
        return {"id": sid, "status": status}


class FakeEvents:
    def __init__(self):
        self.seen = {}
        self.rows = []

    def record(self, eid, etype, caller_id, kind, subject_id, payload):
        if eid in self.seen:
            return None
        self.seen[eid] = True
        self.rows.append({"id": len(self.rows) + 1, "caller_id": caller_id,
                          "event_type": etype, "payload": payload})
        return len(self.rows)


@pytest.fixture
def env(monkeypatch):
    subs, events = FakeSubs(), FakeEvents()
    monkeypatch.setattr(wh.subs_store, "get_by_paypal_id", subs.get_by_paypal_id)
    monkeypatch.setattr(wh.subs_store, "set_status", subs.set_status)
    monkeypatch.setattr(wh.orders_store, "get_by_paypal_id", lambda p: None)
    monkeypatch.setattr(wh.events_store, "record", events.record)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False), subs, events


def _ok_verify(monkeypatch, result=True, spy=None):
    def verify(raw, headers):
        if spy is not None:
            spy.append(raw)
        return result
    monkeypatch.setattr(wh.verifier, "verify", verify)


def test_unconfigured_webhook_id_returns_503(env, monkeypatch, fake_settings):
    client, _, _ = env
    monkeypatch.setattr(fake_settings, "paypal_webhook_id", None, raising=False)
    r = client.post("/v1/webhooks", json={"id": "e1"})
    assert r.status_code == 503


def test_bad_signature_is_401_and_not_stored(env, monkeypatch):
    client, _, events = env
    _ok_verify(monkeypatch, result=False)
    r = client.post("/v1/webhooks", json={"id": "e-bad", "event_type": "X"})
    assert r.status_code == 401
    assert events.rows == []          # 驗簽失敗不落地


def test_verification_receives_raw_bytes(env, monkeypatch):
    client, _, _ = env
    spy = []
    _ok_verify(monkeypatch, spy=spy)
    body = b'{"id":"e2",  "event_type":"PAYMENT.SALE.COMPLETED","resource":{}}'
    client.post("/v1/webhooks", content=body,
                headers={"Content-Type": "application/json"})
    # 逐位元組相同 —— 不是重新序列化過的（注意 body 裡刻意有多餘空白）
    assert spy[0] == body


def test_duplicate_event_is_noop(env, monkeypatch):
    client, _, events = env
    _ok_verify(monkeypatch)
    ev = {"id": "e3", "event_type": "PAYMENT.SALE.COMPLETED", "resource": {}}
    assert client.post("/v1/webhooks", json=ev).json()["status"] == "ok"
    assert client.post("/v1/webhooks", json=ev).json()["status"] == "duplicate"
    assert len(events.rows) == 1


def test_unmappable_event_stored_with_null_caller(env, monkeypatch):
    client, _, events = env
    _ok_verify(monkeypatch)
    client.post("/v1/webhooks", json={
        "id": "e4", "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"billing_agreement_id": "I-UNKNOWN"}})
    assert events.rows[0]["caller_id"] is None


def test_subscription_payment_marks_active(env, monkeypatch):
    client, subs, _ = env
    _ok_verify(monkeypatch)
    row = subs.add("I-ABC")
    client.post("/v1/webhooks", json={
        "id": "e5", "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"billing_agreement_id": "I-ABC"}})
    assert subs.status_calls == [(row["id"], "ACTIVE")]


def test_subscription_cancelled_updates_status(env, monkeypatch):
    client, subs, _ = env
    _ok_verify(monkeypatch)
    row = subs.add("I-DEF")
    client.post("/v1/webhooks", json={
        "id": "e6", "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
        "resource": {"id": "I-DEF"}})
    assert subs.status_calls == [(row["id"], "CANCELLED")]


def test_payment_failed_does_not_change_status(env, monkeypatch):
    """扣款失敗不等於訂閱結束 —— PayPal 會依門檻重試。"""
    client, subs, events = env
    _ok_verify(monkeypatch)
    subs.add("I-GHI")
    client.post("/v1/webhooks", json={
        "id": "e7", "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
        "resource": {"id": "I-GHI"}})
    assert subs.status_calls == []
    assert len(events.rows) == 1       # 但事件有記錄，caller 拉得到
