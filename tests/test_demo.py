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

    def list_(caller_id, status=None, limit=50, offset=0, tx=None):
        # ⚠️ 這裡刻意**照著真的 store 簽名**收 limit/offset。
        # 直接呼叫 router 函式時，帶 Query(default=...) 的參數不會被解析，
        # 那個 Query 物件會原樣傳到這裡 —— 假的 store 如果用 **kwargs 吞掉，
        # 就永遠測不出那個 bug。
        assert isinstance(limit, int), f"limit 不是 int 是 {limit!r}"
        assert offset is None or isinstance(offset, int)
        return [dict(r) for r in rows.values() if r["caller_id"] == caller_id]

    monkeypatch.setattr(orders_router.store, "list_", list_)
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


import time

from app.webhooks import signing


@pytest.fixture
def push_env(monkeypatch, fake_settings):
    """推送端點的 store 換掉，讓 PUT /v1/webhook-endpoint 不碰 DB。"""
    from app.routers import push as push_router

    saved = {}

    def upsert(caller_id, url, tx=None):
        saved.update(id="ep-1", caller_id=caller_id, url=url, active=True,
                     updated_at="2026-08-27T00:00:00Z")
        return dict(saved)

    monkeypatch.setattr(push_router.endpoints_store, "upsert", upsert)
    monkeypatch.setattr(push_router.endpoints_store, "get",
                        lambda cid, tx=None: dict(saved) or None)
    return saved


def _signed(raw: bytes, *, t=None, secret=None):
    t = int(time.time()) if t is None else t
    secret = secret or signing.secret_for("demo")
    return signing.header(secret, t, raw)


def test_啟用推送會把端點指回自己的sink(push_env, fake_settings):
    out = flows.enable_push("https://demo.example")
    assert out["url"] == "https://demo.example/demo/sink"
    assert push_env["caller_id"] == "demo"


def test_sink收下正確簽章(fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    raw = b'{"id":1,"event_type":"PAYMENT.CAPTURE.COMPLETED"}'
    r = client.post("/demo/sink", content=raw,
                    headers={"X-Signature": _signed(raw),
                             "Content-Type": "application/json"})
    assert r.status_code == 200


def test_sink擋掉錯的簽章(fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    raw = b'{"id":1,"event_type":"X"}'
    bad = f"t={int(time.time())},v1={'0' * 64}"
    r = client.post("/demo/sink", content=raw, headers={"X-Signature": bad})
    assert r.status_code == 401


def test_sink擋掉沒有簽章的(fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    assert client.post("/demo/sink", content=b"{}").status_code == 401


def test_sink擋掉過期的時間戳(fake_settings):
    """防重放。容忍度是 signing.TOLERANCE_SECONDS。"""
    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    raw = b'{"id":1,"event_type":"X"}'
    old = int(time.time()) - signing.TOLERANCE_SECONDS - 60
    r = client.post("/demo/sink", content=raw,
                    headers={"X-Signature": _signed(raw, t=old)})
    assert r.status_code == 401


def test_sink驗的是原始bytes(fake_settings):
    """⚠️ 重新序列化的 JSON 跟原文不保證逐位元組相同 ——
    而且只在有非 ASCII 的 payload 上才發作。"""
    import json

    probe = _probe_app(fake_settings, "sandbox")
    client = TestClient(probe, raise_server_exceptions=False)
    original = '{"msg":"付款成功"}'.encode()
    reserialized = json.dumps(json.loads(original)).encode()
    assert original != reserialized
    r = client.post("/demo/sink", content=original,
                    headers={"X-Signature": _signed(reserialized)})
    assert r.status_code == 401


def test_state只回demo自己的資料(orders_env, fake_settings, monkeypatch):
    """每張業務表都有 caller_id，隔離是查詢層的預設。
    這條測試釘住「demo 不會看到別人的東西」。"""
    from app.routers import orders as orders_router

    seen = {}

    def list_(caller_id, status=None, limit=50, offset=0, tx=None):
        seen["orders"] = caller_id
        return []
    monkeypatch.setattr(orders_router.store, "list_", list_)
    monkeypatch.setattr(flows, "_subscriptions", lambda: [])
    monkeypatch.setattr(flows, "_events", lambda: [])
    monkeypatch.setattr(flows, "_deliveries", lambda: [])

    out = flows.state()
    assert seen["orders"] == "demo"
    assert set(out) >= {"orders", "subscriptions", "events", "deliveries",
                        "push_configured"}


def test_推送未設定時state不會爆掉(fake_settings, monkeypatch):
    """/v1/deliveries 在推送未設定時回 503。畫面不該因此整個壞掉 ——
    它還要能顯示訂單。"""
    monkeypatch.setattr(fake_settings, "push_configured", False)
    monkeypatch.setattr(flows, "_orders", lambda: [])
    monkeypatch.setattr(flows, "_subscriptions", lambda: [])
    monkeypatch.setattr(flows, "_events", lambda: [])

    out = flows.state()
    assert out["push_configured"] is False
    assert out["deliveries"] == []


def test_頁面吐得出完整HTML(fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    body = TestClient(probe).get("/demo").text
    assert "單筆付款" in body and "訂閱付款" in body
    assert "/demo/api/state" in body        # 頁面真的會去輪詢


def test_ping走真的佇列(push_env, fake_settings, monkeypatch):
    """⚠️ 同步直送會跳過 Cloud Tasks、內部端點、X-Internal-Key、重試 ——
    而那四樣正好是最會壞的部分。這顆按鈕存在的意義就是「不用真的付一筆錢
    也能驗完整條路」，跳過那些就沒有意義了。"""
    from app.webhooks import dispatch

    enqueued = []
    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: {"id": "ep-1",
                                              "url": "https://x.example/sink"})
    monkeypatch.setattr(dispatch.deliveries_store, "create",
                        lambda *a, **k: {"id": "d-1", "caller_id": "demo"})
    monkeypatch.setattr(dispatch.deliveries_store, "get",
                        lambda did, tx=None: {"id": did, "status": "pending"})
    monkeypatch.setattr(dispatch.tasks, "enqueue_delivery",
                        lambda cid, url: enqueued.append((cid, url)))

    out = flows.send_ping("https://demo.example")
    assert out["delivery_id"] == "d-1"
    assert enqueued and enqueued[0][0] == "demo"
    assert "/internal/deliveries/d-1" in enqueued[0][1]


def test_沒註冊端點就不能ping(fake_settings, monkeypatch):
    from fastapi import HTTPException

    from app.webhooks import dispatch

    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: None)
    with pytest.raises(HTTPException) as e:
        flows.send_ping("https://demo.example")
    assert e.value.status_code == 400


def test_state真的讀得到資料_不是被_safe吞掉(orders_env, fake_settings, monkeypatch):
    """⚠️ 這條是迴歸測試，抓的是一個實跑 dev 才發現的 bug。

    直接呼叫 router 函式時，帶 `Query(default=...)` 的參數**不會**被解析成
    預設值 —— 那個 Query 物件會原樣傳下去，最後變成 SQL 的參數，
    症狀是 `invalid input syntax for type bigint: "annotation=int ..."`。

    而 state() 的 _safe() 會把它吞掉並回 []，所以畫面**看起來只是空的**，
    HTTP 還是 200。前一版的測試因為直接 monkeypatch 掉 _orders/_deliveries，
    完全沒有執行到真正的那條路。

    所以這條只在 store 那一層放假的，中間的 router 函式一律**真的跑過**。
    """
    from app.routers import push as push_router

    monkeypatch.setattr(
        push_router.deliveries_store, "list_for_caller",
        lambda caller_id, event_id=None, status=None, limit=100, tx=None: [{
            "id": "d-1", "event_id": None, "endpoint_id": "ep-1",
            "caller_id": caller_id, "url": "https://x.example/sink",
            "status": "delivered", "attempts": 1, "last_status": 200,
            "last_error": None, "created_at": "2026-08-27T00:00:00Z",
            "updated_at": "2026-08-27T00:00:00Z",
            "delivered_at": "2026-08-27T00:00:00Z"}])
    monkeypatch.setattr(flows, "_subscriptions", lambda: [])
    monkeypatch.setattr(flows, "_events", lambda: [])
    monkeypatch.setattr(flows.endpoints_store, "get", lambda cid, tx=None: None)

    flows.start_order("10.00", "https://demo.example")
    out = flows.state()

    assert len(out["orders"]) == 1, "訂單被 _safe 吞掉了"
    assert out["orders"][0]["amount"] == "10.00"
    assert len(out["deliveries"]) == 1, "投遞紀錄被 _safe 吞掉了"
    assert out["deliveries"][0]["status"] == "delivered"
