"""付款模擬頁。

⚠️ 這裡最重要的一條是「live 環境根本沒有這些路由」——
prod 是 live，一筆 demo 訂單就是一筆真的收款。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _probe_app(fake_settings, paypal_env: str) -> FastAPI:
    """用指定的 paypal_env 重新掛一次 router，回一個乾淨的 app。

    ⚠️ 不用 importlib.reload(app.main) —— 那會把模組狀態留給後面的測試。
    掛載決定抽成 mount_routers(app) 就是為了讓這件事測得起來。

    ⚠️ `app.main` 一定要在**函式裡面**才 import。它在模組層就會跑
    `mount_routers(app)`，而那會呼叫 get_settings() —— 在測試模組頂端 import
    的話，那一行發生在 autouse 的 fake_settings fixture 之前，
    會去讀真的環境變數然後死在「缺少必要環境變數 PAYPAL_ENV」。
    """
    import app.main as main

    fake_settings.paypal_env = paypal_env
    probe = FastAPI()
    main.mount_routers(probe)
    return probe


def test_live環境沒有demo路由(fake_settings):
    """prod 是 live。這條測試就是 prod 隔離的全部實作。"""
    probe = _probe_app(fake_settings, "live")
    assert "/demo" not in {r.path for r in probe.routes}
    assert TestClient(probe).get("/demo").status_code == 404


def test_sandbox環境掛得出頁面(fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    r = TestClient(probe).get("/demo")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_demo身分固定是demo(fake_settings):
    from app.demo.flows import DEMO_CALLER, DEMO_CALLER_ID

    assert DEMO_CALLER_ID == "demo"
    assert DEMO_CALLER.caller_id == "demo"


import pytest

from app.demo import flows


class FakePayPalOrders:
    """記下送給 PayPal 的東西，並回一個像樣的訂單。"""

    def __init__(self):
        self.created = []
        self.captured = []

    def create_order(self, **kw):
        self.created.append(kw)
        return {"id": "PP-ORDER-1", "status": "CREATED",
                "links": [{"rel": "approve",
                           "href": "https://sandbox.paypal.com/checkoutnow?token=PP-ORDER-1"}]}

    def capture_order(self, pp_id):
        self.captured.append(pp_id)
        return {"id": pp_id, "status": "COMPLETED"}


@pytest.fixture
def orders_env(monkeypatch):
    """把 orders router 用到的 store 與 PayPal 全部換掉。"""
    from app.routers import orders as orders_router

    pp = FakePayPalOrders()
    rows = {}

    def create(caller_id, reference_id, amount, currency, status, tx=None):
        row = {"id": f"local-{len(rows) + 1}", "caller_id": caller_id,
               "reference_id": reference_id, "amount": amount,
               "currency": currency, "status": status,
               "paypal_order_id": None, "created_at": "2026-08-27T00:00:00Z",
               "captured_at": None}
        rows[reference_id] = row
        return dict(row)

    def get_by_reference(caller_id, reference_id, tx=None):
        row = rows.get(reference_id)
        return dict(row) if row and row["caller_id"] == caller_id else None

    def attach(order_id, pp_id, status, tx=None):
        row = next(r for r in rows.values() if r["id"] == order_id)
        row.update(paypal_order_id=pp_id, status=status)
        return dict(row)

    def get(caller_id, order_id, tx=None):
        row = next((r for r in rows.values() if r["id"] == order_id), None)
        return dict(row) if row and row["caller_id"] == caller_id else None

    def set_status(order_id, status, captured=False, tx=None):
        row = next(r for r in rows.values() if r["id"] == order_id)
        row["status"] = status
        return dict(row)

    monkeypatch.setattr(orders_router.store, "create", create)
    monkeypatch.setattr(orders_router.store, "get_by_reference", get_by_reference)
    monkeypatch.setattr(orders_router.store, "attach_paypal_id", attach)
    monkeypatch.setattr(orders_router.store, "get", get)
    monkeypatch.setattr(orders_router.store, "set_status", set_status)
    monkeypatch.setattr(orders_router.pp, "create_order", pp.create_order)
    monkeypatch.setattr(orders_router.pp, "capture_order", pp.capture_order)
    return pp


def test_建單帶著我們自己的導回網址(orders_env, fake_settings):
    out = flows.start_order("12.34", "https://demo.example")
    ref = out["reference_id"]
    ctx = orders_env.created[0]
    assert ctx["return_url"] == f"https://demo.example/demo/return/order/{ref}"
    assert ctx["cancel_url"] == f"https://demo.example/demo/cancel/order/{ref}"
    assert out["approve_url"].startswith("https://sandbox.paypal.com/")


def test_建單走的是v1的金額驗證(orders_env, fake_settings):
    """金額錯誤要回 400 帶欄位名 —— 那是 /v1 那份邏輯給的，
    demo 沒有自己再寫一份。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        flows.start_order("12.345", "https://demo.example")
    assert e.value.status_code == 400
    assert e.value.detail["field"] == "amount"


def test_導回時會capture(orders_env, fake_settings):
    out = flows.start_order("10.00", "https://demo.example")
    assert flows.finish_order(out["reference_id"]) == "paid"
    assert orders_env.captured == ["PP-ORDER-1"]


def test_導回時找不到那筆單就說missing(orders_env, fake_settings):
    assert flows.finish_order("demo-不存在") == "missing"


def test_導回端點回302導向demo頁(orders_env, fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    out = flows.start_order("10.00", "https://demo.example")
    r = client.get(f"/demo/return/order/{out['reference_id']}",
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/demo?")
    assert "result=paid" in r.headers["location"]


def test_取消端點不會capture(orders_env, fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    out = flows.start_order("10.00", "https://demo.example")
    r = client.get(f"/demo/cancel/order/{out['reference_id']}",
                   follow_redirects=False)
    assert r.status_code == 303
    assert "result=cancelled" in r.headers["location"]
    assert orders_env.captured == []
