import pytest
from fastapi import HTTPException

from app.auth import hash_key, require, Caller


def test_hash_is_sha256():
    assert hash_key("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def _dep(monkeypatch, row):
    import app.auth as a
    monkeypatch.setattr(a.api_keys, "lookup", lambda h: row)
    monkeypatch.setattr(a.api_keys, "touch", lambda i: None)
    return require("orders:read")


def test_missing_key_is_401(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _dep(monkeypatch, None)(x_api_key=None)
    assert e.value.status_code == 401


def test_unknown_key_is_401(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _dep(monkeypatch, None)(x_api_key="nope")
    assert e.value.status_code == 401


def test_inactive_key_is_401(monkeypatch):
    row = {"id": "1", "caller_id": "c", "scopes": ["orders:read"], "active": False}
    with pytest.raises(HTTPException) as e:
        _dep(monkeypatch, row)(x_api_key="k")
    assert e.value.status_code == 401


def test_inactive_and_unknown_are_indistinguishable(monkeypatch):
    row = {"id": "1", "caller_id": "c", "scopes": ["orders:read"], "active": False}
    with pytest.raises(HTTPException) as a:
        _dep(monkeypatch, row)(x_api_key="k")
    with pytest.raises(HTTPException) as b:
        _dep(monkeypatch, None)(x_api_key="k")
    assert a.value.detail == b.value.detail


def test_insufficient_scope_is_403_and_names_it(monkeypatch):
    row = {"id": "1", "caller_id": "c", "scopes": ["orders:write"], "active": True}
    with pytest.raises(HTTPException) as e:
        _dep(monkeypatch, row)(x_api_key="k")
    assert e.value.status_code == 403
    assert "orders:read" in e.value.detail


def test_valid_key_returns_caller(monkeypatch):
    row = {"id": "1", "caller_id": "shop", "scopes": ["orders:read"], "active": True}
    caller = _dep(monkeypatch, row)(x_api_key="k")
    assert caller == Caller("shop", frozenset({"orders:read"}))


def test_last_used_is_touched(monkeypatch):
    import app.auth as a
    touched = []
    row = {"id": "42", "caller_id": "c", "scopes": ["orders:read"], "active": True}
    monkeypatch.setattr(a.api_keys, "lookup", lambda h: row)
    monkeypatch.setattr(a.api_keys, "touch", lambda i: touched.append(i))
    require("orders:read")(x_api_key="k")
    assert touched == ["42"]
