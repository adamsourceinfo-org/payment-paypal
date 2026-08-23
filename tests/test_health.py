"""健康檢查必須讓 CI 的 smoke 擋得住壞掉的部署。

ci 的 smoke 只看 HTTP 狀態碼（200-399 就算過），完全不看內容。
所以「回 200 但 body 裡寫著 db 掛了」對 CI 來說是綠燈 —— 那個健康檢查什麼也沒證明。
"""
import pytest
from fastapi.testclient import TestClient

import app.db as db_mod
import app.paypal.client as pc


def _client(monkeypatch, *, db_ok=True, token="ok", webhook="WH-1",
            db_configured=True, settings=None):
    monkeypatch.setattr(settings, "paypal_webhook_id", webhook)
    monkeypatch.setattr(settings, "db_configured", db_configured)
    monkeypatch.setattr(db_mod, "db_status", lambda: (
        {"configured": True, "ok": True, "instance": "i",
         "server_user": "u", "database": "d"} if db_ok else
        {"configured": True, "ok": False, "instance": "i", "error": "boom"}
    ) if db_configured else {"configured": False})
    monkeypatch.setattr(pc, "token_status", lambda: token)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_all_good_is_200(monkeypatch, fake_settings):
    r = _client(monkeypatch, settings=fake_settings).get("/health")
    assert r.status_code == 200
    assert r.json()["db"]["ok"] is True


def test_db_down_is_503(monkeypatch, fake_settings):
    r = _client(monkeypatch, db_ok=False, settings=fake_settings).get("/health")
    assert r.status_code == 503, "DB 掛掉卻回 200 的話，CI 會綠燈通過一個壞掉的部署"
    assert r.json()["db"]["ok"] is False


def test_bad_paypal_credentials_is_503(monkeypatch, fake_settings):
    r = _client(monkeypatch, token="error:401", settings=fake_settings).get("/health")
    assert r.status_code == 503, "憑證錯卻回 200 的話，prod 會綠燈上線但收不了錢"


def test_unconfigured_db_is_503(monkeypatch, fake_settings):
    r = _client(monkeypatch, db_configured=False, settings=fake_settings).get("/health")
    assert r.status_code == 503


def test_missing_webhook_is_still_200(monkeypatch, fake_settings):
    """webhook 未設定是刻意允許的既定狀態 —— 雞生蛋：
    第一次部署才拿得到 Cloud Run URL，有 URL 才能去 PayPal 註冊 webhook。
    這一步必須能綠燈，否則永遠部署不出去。"""
    r = _client(monkeypatch, webhook=None, settings=fake_settings).get("/health")
    assert r.status_code == 200
    assert r.json()["paypal"]["webhook"] == "unconfigured"


def test_never_leaks_credentials(monkeypatch, fake_settings):
    raw = _client(monkeypatch, settings=fake_settings).get("/health").text
    assert fake_settings.paypal_client_secret not in raw
    assert "Bearer" not in raw
