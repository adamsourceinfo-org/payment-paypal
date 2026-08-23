import pytest
from fastapi.testclient import TestClient

import app.auth as auth_mod
import app.routers.events as ev

KEY = "k"
CALLER = "c1"


class FakeEvents:
    def __init__(self):
        self.rows = [
            {"id": 1, "caller_id": CALLER, "event_type": "A", "subject_kind": None,
             "subject_id": None, "payload": {}, "received_at": None},
            {"id": 2, "caller_id": CALLER, "event_type": "B", "subject_kind": None,
             "subject_id": None, "payload": {}, "received_at": None},
            {"id": 3, "caller_id": None, "event_type": "ORPHAN",
             "subject_kind": None, "subject_id": None, "payload": {},
             "received_at": None},
            {"id": 4, "caller_id": "other", "event_type": "THEIRS",
             "subject_kind": None, "subject_id": None, "payload": {},
             "received_at": None},
            {"id": 5, "caller_id": CALLER, "event_type": "C", "subject_kind": None,
             "subject_id": None, "payload": {}, "received_at": None},
        ]

    def list_after(self, caller_id, after, limit):
        # 刻意逐字模仿 SQL 的 WHERE caller_id = %s：NULL 不匹配任何人
        return [r for r in self.rows
                if r["caller_id"] == caller_id and r["id"] > after][:limit]


@pytest.fixture
def client(monkeypatch):
    fake = FakeEvents()
    monkeypatch.setattr(ev.store, "list_after", fake.list_after)
    monkeypatch.setattr(auth_mod.api_keys, "lookup", lambda h: {
        "id": "k1", "caller_id": CALLER, "scopes": ["events:read"], "active": True}
        if h == auth_mod.hash_key(KEY) else None)
    monkeypatch.setattr(auth_mod.api_keys, "touch", lambda i: None)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


H = {"X-API-Key": KEY}


def test_cursor_returns_only_newer(client):
    first = client.get("/v1/events?after=0&limit=2", headers=H).json()
    assert [i["id"] for i in first["items"]] == [1, 2]
    assert first["next_cursor"] == 2
    nxt = client.get("/v1/events?after=2", headers=H).json()
    assert [i["id"] for i in nxt["items"]] == [5]


def test_null_caller_events_invisible(client):
    ids = [i["id"] for i in client.get("/v1/events?after=0", headers=H).json()["items"]]
    assert 3 not in ids


def test_other_callers_events_invisible(client):
    ids = [i["id"] for i in client.get("/v1/events?after=0", headers=H).json()["items"]]
    assert 4 not in ids


def test_limit_over_max_is_422(client):
    # FastAPI 的 Query(le=500) 會擋在驗證層
    assert client.get("/v1/events?after=0&limit=501", headers=H).status_code == 422


def test_events_requires_scope(client, monkeypatch):
    monkeypatch.setattr(auth_mod.api_keys, "lookup", lambda h: {
        "id": "k1", "caller_id": CALLER, "scopes": ["orders:read"], "active": True})
    assert client.get("/v1/events?after=0", headers=H).status_code == 403
