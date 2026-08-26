"""PayPal 打進來的 webhook 接收器。

這支是全服務唯一的 `async def` handler，而它裡面有兩件會擋很久的事：
驗簽的對外 HTTP、以及同步的 pg8000。
所以這裡除了業務邏輯之外，還釘住兩件結構性的事：

- 處理**不在事件迴圈上**跑
- 落地事件與更新狀態在**同一個交易**裡
"""
import asyncio
import json
import threading
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import app.routers.webhooks as wh


class FakeSubs:
    def __init__(self, timeline):
        self.rows = {}
        self.status_calls = []
        self.timeline = timeline

    def add(self, paypal_id, caller="c1"):
        r = {"id": uuid.uuid4(), "caller_id": caller,
             "paypal_subscription_id": paypal_id, "status": "APPROVAL_PENDING"}
        self.rows[paypal_id] = r
        return r

    def get_by_paypal_id(self, pid, tx=None):
        return self.rows.get(pid)

    def set_status(self, sid, status, period_end=None, tx=None):
        self.status_calls.append((sid, status))
        self.timeline.append("更新訂閱狀態")
        return {"id": sid, "status": status}


class FakeEvents:
    def __init__(self):
        self.seen = {}
        self.rows = []

    def record(self, eid, etype, caller_id, kind, subject_id, payload, tx=None):
        if eid in self.seen:
            return None
        self.seen[eid] = True
        self.rows.append({"id": len(self.rows) + 1, "caller_id": caller_id,
                          "event_type": etype, "payload": payload})
        return len(self.rows)


@pytest.fixture
def env(monkeypatch):
    timeline = []
    subs, events = FakeSubs(timeline), FakeEvents()
    monkeypatch.setattr(wh.subs_store, "get_by_paypal_id", subs.get_by_paypal_id)
    monkeypatch.setattr(wh.subs_store, "set_status", subs.set_status)
    monkeypatch.setattr(wh.orders_store, "get_by_paypal_id",
                        lambda p, tx=None: None)
    monkeypatch.setattr(wh.events_store, "record", events.record)

    # 入站現在把「落地事件 + 更新狀態」包成一個交易。這裡不需要真的連 DB，
    # 但 transaction() 一定要被呼叫到 —— 不然測的就不是實際跑的那條路。
    counters = {"transactions": 0}

    @contextmanager
    def _fake_tx():
        counters["transactions"] += 1
        yield object()
        timeline.append("交易 commit")
    monkeypatch.setattr(wh.db, "transaction", _fake_tx)

    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    return {"client": client, "subs": subs, "events": events,
            "timeline": timeline, "counters": counters}


def _ok_verify(monkeypatch, result=True, spy=None):
    def verify(raw, headers):
        if spy is not None:
            spy.append(raw)
        return result
    monkeypatch.setattr(wh.verifier, "verify", verify)


# --- 既有行為（一個位元組都不該變）-----------------------------------

def test_unconfigured_webhook_id_returns_503(env, monkeypatch, fake_settings):
    monkeypatch.setattr(fake_settings, "paypal_webhook_id", None, raising=False)
    r = env["client"].post("/v1/webhooks", json={"id": "e1"})
    assert r.status_code == 503


def test_bad_signature_is_401_and_not_stored(env, monkeypatch):
    _ok_verify(monkeypatch, result=False)
    r = env["client"].post("/v1/webhooks", json={"id": "e-bad", "event_type": "X"})
    assert r.status_code == 401
    assert env["events"].rows == []          # 驗簽失敗不落地


def test_verification_receives_raw_bytes(env, monkeypatch):
    spy = []
    _ok_verify(monkeypatch, spy=spy)
    body = b'{"id":"e2",  "event_type":"PAYMENT.SALE.COMPLETED","resource":{}}'
    env["client"].post("/v1/webhooks", content=body,
                       headers={"Content-Type": "application/json"})
    # 逐位元組相同 —— 不是重新序列化過的（注意 body 裡刻意有多餘空白）
    assert spy[0] == body


def test_duplicate_event_is_noop(env, monkeypatch):
    client = env["client"]
    _ok_verify(monkeypatch)
    ev = {"id": "e3", "event_type": "PAYMENT.SALE.COMPLETED", "resource": {}}
    assert client.post("/v1/webhooks", json=ev).json()["status"] == "ok"
    assert client.post("/v1/webhooks", json=ev).json()["status"] == "duplicate"
    assert len(env["events"].rows) == 1


def test_unmappable_event_stored_with_null_caller(env, monkeypatch):
    _ok_verify(monkeypatch)
    env["client"].post("/v1/webhooks", json={
        "id": "e4", "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"billing_agreement_id": "I-UNKNOWN"}})
    assert env["events"].rows[0]["caller_id"] is None


def test_subscription_payment_marks_active(env, monkeypatch):
    _ok_verify(monkeypatch)
    row = env["subs"].add("I-ABC")
    env["client"].post("/v1/webhooks", json={
        "id": "e5", "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"billing_agreement_id": "I-ABC"}})
    assert env["subs"].status_calls == [(row["id"], "ACTIVE")]


def test_subscription_cancelled_updates_status(env, monkeypatch):
    _ok_verify(monkeypatch)
    row = env["subs"].add("I-DEF")
    env["client"].post("/v1/webhooks", json={
        "id": "e6", "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
        "resource": {"id": "I-DEF"}})
    assert env["subs"].status_calls == [(row["id"], "CANCELLED")]


def test_payment_failed_does_not_change_status(env, monkeypatch):
    """扣款失敗不等於訂閱結束 —— PayPal 會依門檻重試。"""
    _ok_verify(monkeypatch)
    env["subs"].add("I-GHI")
    env["client"].post("/v1/webhooks", json={
        "id": "e7", "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
        "resource": {"id": "I-GHI"}})
    assert env["subs"].status_calls == []
    assert len(env["events"].rows) == 1       # 但事件有記錄，caller 拉得到


# --- 併發模型 ---------------------------------------------------------

def test_處理不在事件迴圈上跑(env, monkeypatch):
    """⚠️ 這一支是全服務唯一的 async handler，而它裡面有驗簽的對外 HTTP
    與同步的 pg8000。跑在事件迴圈上的話，
    那個實例上**所有**請求跟著排隊 —— 包括 caller 正在查的
    `GET /v1/orders/{id}`。行銷活動當天，一筆 webhook 就是幾百毫秒的全實例停擺。

    所以最外層只 `await request.body()`，其餘丟 threadpool。
    """
    seen = {}

    def verify(raw, headers):
        seen["thread"] = threading.current_thread()
        seen["raw_type"] = type(raw)
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return True
    monkeypatch.setattr(wh.verifier, "verify", verify)

    env["client"].post("/v1/webhooks", json={
        "id": "e-thread", "event_type": "PAYMENT.SALE.COMPLETED", "resource": {}})

    assert seen["raw_type"] is bytes          # 拿到的是原始 bytes
    assert seen["on_loop"] is False           # 而且不在事件迴圈的執行緒上


# --- 交易與排程時機 ---------------------------------------------------

def test_落地與狀態更新在同一個交易裡(env, monkeypatch):
    """分成兩次 commit 的話，中間掛掉時 PayPal 會重送，但重送會被
    paypal_event_id 的唯一鍵擋掉 → record() 回 None → 早退 →
    **狀態更新永遠不會執行**。去重鍵一邊做著它該做的事，
    一邊堵死了唯一的復原路徑。"""
    _ok_verify(monkeypatch)
    env["subs"].add("I-TX")
    env["client"].post("/v1/webhooks", json={
        "id": "e-tx", "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"billing_agreement_id": "I-TX"}})
    assert env["counters"]["transactions"] == 1


def test_事件內容原文落地(env, monkeypatch):
    """推送與拉取送的都是這一份原文，所以它必須是 PayPal 送來的字串本身。"""
    _ok_verify(monkeypatch)
    body = b'{"id":"e-raw","event_type":"PAYMENT.SALE.COMPLETED","resource":{}}'
    env["client"].post("/v1/webhooks", content=body,
                       headers={"Content-Type": "application/json"})
    assert json.loads(env["events"].rows[0]["payload"])["id"] == "e-raw"
