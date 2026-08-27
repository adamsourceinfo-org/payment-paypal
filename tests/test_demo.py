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


@pytest.fixture
def subs_env(monkeypatch):
    """方案 + 訂閱兩條 store，加上假的 PayPal。"""
    from app.routers import plans as plans_router
    from app.routers import subscriptions as subs_router

    plans, subs = {}, {}
    calls = {"products": 0, "plans": 0, "subs": []}

    def create_product(name, description=None):
        calls["products"] += 1
        return {"id": f"PROD-{calls['products']}"}

    def create_plan(product_id, name, amount, currency, interval_count,
                    description=None):
        calls["plans"] += 1
        return {"id": f"PLAN-{calls['plans']}"}

    def plan_store_create(caller_id, prod, pp_plan, name, amount, currency,
                          interval_count, tx=None):
        row = {"id": f"plan-{len(plans) + 1}", "caller_id": caller_id,
               "paypal_product_id": prod, "paypal_plan_id": pp_plan,
               "name": name, "amount": amount, "currency": currency,
               "interval_unit": "MONTH", "interval_count": interval_count,
               "status": "ACTIVE", "created_at": "2026-08-27T00:00:00Z"}
        plans[row["id"]] = row
        return dict(row)

    def plan_list(caller_id, limit=50, offset=0, tx=None):
        return [dict(p) for p in plans.values() if p["caller_id"] == caller_id]

    def plan_get(caller_id, plan_id, tx=None):
        p = plans.get(str(plan_id))
        return dict(p) if p and p["caller_id"] == caller_id else None

    monkeypatch.setattr(plans_router.pp, "create_product", create_product)
    monkeypatch.setattr(plans_router.pp, "create_plan", create_plan)
    monkeypatch.setattr(plans_router.store, "create", plan_store_create)
    monkeypatch.setattr(plans_router.store, "list_", plan_list)
    monkeypatch.setattr(subs_router.plans_store, "get", plan_get)

    def sub_create(caller_id, plan_id, reference_id, status, tx=None):
        row = {"id": f"sub-{len(subs) + 1}", "caller_id": caller_id,
               "plan_id": plan_id, "reference_id": reference_id,
               "status": status, "paypal_subscription_id": None,
               "current_period_end": None,
               "created_at": "2026-08-27T00:00:00Z"}
        subs[reference_id] = row
        return dict(row)

    def sub_by_ref(caller_id, reference_id, tx=None):
        row = subs.get(reference_id)
        return dict(row) if row and row["caller_id"] == caller_id else None

    def sub_attach(sub_id, pp_id, status, tx=None):
        row = next(r for r in subs.values() if r["id"] == sub_id)
        row.update(paypal_subscription_id=pp_id, status=status)
        return dict(row)

    def pp_create_sub(**kw):
        calls["subs"].append(kw)
        return {"id": "PP-SUB-1", "status": "APPROVAL_PENDING",
                "links": [{"rel": "approve",
                           "href": "https://sandbox.paypal.com/subscribe?ba=PP-SUB-1"}]}

    monkeypatch.setattr(subs_router.store, "create", sub_create)
    monkeypatch.setattr(subs_router.store, "get_by_reference", sub_by_ref)
    monkeypatch.setattr(subs_router.store, "attach_paypal_id", sub_attach)
    monkeypatch.setattr(subs_router.pp, "create_subscription", pp_create_sub)
    return calls


def test_第一次會建方案第二次重用(subs_env, fake_settings):
    """每按一次訂閱就多一個方案的話，PayPal 後台會被塞滿。"""
    first = flows.ensure_plan()
    second = flows.ensure_plan()
    assert first["id"] == second["id"]
    assert subs_env["plans"] == 1


def test_建訂閱帶著我們自己的導回網址(subs_env, fake_settings):
    out = flows.start_subscription("https://demo.example")
    ref = out["reference_id"]
    ctx = subs_env["subs"][0]
    assert ctx["return_url"] == f"https://demo.example/demo/return/subscription/{ref}"
    assert ctx["cancel_url"] == f"https://demo.example/demo/cancel/subscription/{ref}"
    assert out["approve_url"].startswith("https://sandbox.paypal.com/")


def test_訂閱導回不做capture只導向(subs_env, fake_settings):
    """訂閱沒有 capture 這一步 —— 它靠 webhook BILLING.SUBSCRIPTION.ACTIVATED
    轉成 ACTIVE。導回頁只能說「已送出，等 PayPal 通知」。"""
    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    out = flows.start_subscription("https://demo.example")
    r = client.get(f"/demo/return/subscription/{out['reference_id']}",
                   follow_redirects=False)
    assert r.status_code == 303
    assert "result=subscribed" in r.headers["location"]
