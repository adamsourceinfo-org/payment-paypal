"""健康檢查必須讓 CI 的 smoke 擋得住壞掉的部署。

ci 的 smoke 只看 HTTP 狀態碼（200-399 就算過），完全不看內容。
所以「回 200 但 body 裡寫著 db 掛了」對 CI 來說是綠燈 —— 那個健康檢查什麼也沒證明。
"""
import pytest
from fastapi.testclient import TestClient

import app.db as db_mod
import app.paypal.client as pc
import app.routers.health as health_mod


def _client(monkeypatch, *, db_ok=True, token="ok", webhook="WH-1",
            db_configured=True, settings=None):
    monkeypatch.setattr(settings, "paypal_webhook_id", webhook)
    monkeypatch.setattr(settings, "db_configured", db_configured)
    monkeypatch.setattr(health_mod.endpoints_store, "count_active", lambda: 1)
    monkeypatch.setattr(health_mod.deliveries_store, "dead_count",
                        lambda hours=24: 0)
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


# --- 推送 -------------------------------------------------------------

def test_推送有設定時說得出佇列與端點數(monkeypatch, fake_settings):
    body = _client(monkeypatch, settings=fake_settings).get("/health").json()
    assert body["push"] == {"configured": True,
                            "queue_prefix": "payment-paypal-deliveries",
                            "location": "asia-east1",
                            "endpoints_active": 1, "dead_last_24h": 0}


def test_推送未設定仍然是200(monkeypatch, fake_settings):
    """兩把機密缺席時服務仍然完全可用 —— 事件照樣落地，GET /v1/events 照樣拉得到。
    第一次部署時 secret 還沒建，這一步必須能綠燈。"""
    monkeypatch.setattr(fake_settings, "push_configured", False)
    r = _client(monkeypatch, settings=fake_settings).get("/health")
    assert r.status_code == 200
    assert r.json()["push"] == {"configured": False}


def test_積壓的死信不讓健康檢查變成503(monkeypatch, fake_settings):
    """⚠️ 503 的意思是「這個服務壞了」，而積壓的死信通常代表 **caller** 壞了。
    用 503 表達會讓 ci 的 smoke 從「我們部署成功了嗎」變成
    「所有 caller 今天都好嗎」—— 而後者不是它的工作。"""
    client = _client(monkeypatch, settings=fake_settings)
    monkeypatch.setattr(health_mod.deliveries_store, "dead_count",
                        lambda hours=24: 42)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["push"]["dead_last_24h"] == 42


def test_推送區塊查不動DB時不會炸掉整個健康檢查(monkeypatch, fake_settings):
    """/health 壞掉的時候正是最需要它回答問題的時候。"""
    client = _client(monkeypatch, settings=fake_settings)

    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(health_mod.endpoints_store, "count_active", boom)
    r = client.get("/health")
    assert r.status_code == 200
    assert "connection refused" in r.json()["push"]["error"]


def test_不洩漏推送的兩把機密(monkeypatch, fake_settings):
    raw = _client(monkeypatch, settings=fake_settings).get("/health").text
    assert fake_settings.webhook_signing_key not in raw
    assert fake_settings.internal_key not in raw
