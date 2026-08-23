import app.db as db


def test_token_cached_within_validity(monkeypatch):
    calls = []

    def fake_fetch():
        calls.append(1)
        return "tok-%d" % len(calls), 3600

    monkeypatch.setattr(db, "_fetch_token", fake_fetch)
    db._token_cache = None
    assert db.iam_token() == "tok-1"
    assert db.iam_token() == "tok-1"
    assert len(calls) == 1


def test_token_refetched_when_near_expiry(monkeypatch):
    calls = []

    def fake_fetch():
        calls.append(1)
        return "tok-%d" % len(calls), 30      # 30 秒 < 60 秒門檻

    monkeypatch.setattr(db, "_fetch_token", fake_fetch)
    db._token_cache = None
    assert db.iam_token() == "tok-1"
    assert db.iam_token() == "tok-2"
    assert len(calls) == 2


def test_token_fetch_is_on_new_connection_path(monkeypatch):
    """新連線必須經過 iam_token()，不能用啟動時快取的值。"""
    seen = []
    monkeypatch.setattr(db, "iam_token", lambda: seen.append(1) or "t")
    monkeypatch.setattr(db.pg8000.dbapi, "connect", lambda **kw: kw)
    monkeypatch.setattr(db, "get_settings", lambda: type("S", (), {
        "db_user": "u", "db_name": "d", "db_instance": "i"})())
    db._new_conn()
    db._new_conn()
    assert len(seen) == 2
