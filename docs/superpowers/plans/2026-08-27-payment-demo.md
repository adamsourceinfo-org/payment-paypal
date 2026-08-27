# 付款模擬頁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 payment-paypal 裡加一組**只在 sandbox 存在**的頁面，把單筆付款與訂閱付款從頭到尾走一次，並把事件推送的結果演出來。

**Architecture:** 一個 `app/demo/` 套件。`flows.py` 用合成身分 `Caller(caller_id="demo")` **直接呼叫現有 `/v1/*` router 的函式**（不繞 HTTP、不用 API key、不重寫建單邏輯）；`routes.py` 只負責 HTTP 形狀與推送 sink 的驗簽；`page.html` 是單一靜態檔，靠輪詢 `/demo/api/state` 更新畫面。`app/main.py` 只在 `paypal_env == "sandbox"` 時掛上這個 router。

**Tech Stack:** FastAPI、既有的 `app/routers/*`、`app/webhooks/signing`。**不新增任何相依**（不引 Jinja2）。

**Spec:** `docs/superpowers/specs/2026-08-27-payment-demo-design.md`

## Global Constraints

- **不新增任何 Python 相依。** `requirements.txt` 一個字都不改。頁面是靜態 HTML + 瀏覽器端 fetch。
- **掛載條件是 `get_settings().paypal_env == "sandbox"`**，不是 `app_env`。prod 是 `live`，`/demo` 必須回 404（路由沒註冊），不是靠 route 內的 if 擋。
- **不重寫建單／建訂閱／建方案的邏輯。** 一律呼叫 `app/routers/{orders,subscriptions,plans,push,events}.py` 既有的函式，帶 `DEMO_CALLER`。
- **`caller_id` 固定是 `"demo"`。** 所有 demo 資料靠這個值跟真 caller 隔離。
- **`/demo/sink` 一定要驗簽**，而且驗的是 `await request.body()` 的原始 bytes，不是重新序列化的 JSON。
- **sink 不存任何記憶體狀態。** 多實例下會變成「有時看得到」。畫面一律從 DB 讀。
- 測試一律用 `tests/conftest.py` 的 `fake_settings` fixture，不碰真 DB、不碰真 PayPal。
- Python 3.12（`.venv/bin/python`）。跑測試：`.venv/bin/python -m pytest tests/ -q`。

## File Structure

| 檔案 | 責任 |
|---|---|
| `app/demo/__init__.py` | 空的 |
| `app/demo/flows.py` | `DEMO_CALLER`、建單／建訂閱／確保方案／啟用推送／彙整狀態。**不碰 FastAPI 的 Request/Response** |
| `app/demo/routes.py` | `/demo/*` 的 HTTP 形狀：頁面、JSON API、導回導向、sink 驗簽 |
| `app/demo/page.html` | 單一檔案，CSS/JS 內嵌 |
| `app/main.py`（改） | `_mount()` → `mount_routers(app)`，並加上 sandbox 判斷 |
| `tests/test_demo.py` | 全部 demo 測試 |
| `README.md`（改） | 加一節「模擬頁」，含 sandbox 買家帳號的前置步驟 |

---

### Task 1: demo 身分與 prod 隔離

這一項的交付物是「`/demo` 在 sandbox 出得來、在 live 根本不存在」。頁面內容先給一個最小的殼，Task 5 才長成真的畫面。

**Files:**
- Create: `app/demo/__init__.py`, `app/demo/flows.py`, `app/demo/routes.py`, `app/demo/page.html`
- Modify: `app/main.py`
- Test: `tests/test_demo.py`

**Interfaces:**
- Consumes: `app.auth.Caller`、`app.config.get_settings`
- Produces:
  - `app.demo.flows.DEMO_CALLER_ID: str`（值是 `"demo"`）
  - `app.demo.flows.DEMO_CALLER: Caller`
  - `app.demo.routes.router: APIRouter`（prefix `/demo`）
  - `app.main.mount_routers(app: FastAPI) -> None`

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_demo.py`：

```python
"""付款模擬頁。

⚠️ 這裡最重要的一條是「live 環境根本沒有這些路由」——
prod 是 live，一筆 demo 訂單就是一筆真的收款。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main


def _probe_app(fake_settings, paypal_env: str) -> FastAPI:
    """用指定的 paypal_env 重新掛一次 router，回一個乾淨的 app。

    ⚠️ 不用 importlib.reload(app.main) —— 那會把模組狀態留給後面的測試。
    掛載決定抽成 mount_routers(app) 就是為了讓這件事測得起來。
    """
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
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: FAIL —— `AttributeError: module 'app.main' has no attribute 'mount_routers'`

- [ ] **Step 3: 把掛載決定抽成 mount_routers**

改 `app/main.py`，把 `_mount()` 換成：

```python
def mount_routers(target: FastAPI) -> None:
    """把所有 router 掛到 target 上。

    ⚠️ webhooks 是**入站**（PayPal 打進來），push 是**出站**（我們打給 caller）。
    兩支不同方向、不同認證，名字刻意分開。

    ⚠️ **demo 只在 sandbox 掛。** 判斷條件是 paypal_env 不是 app_env ——
    真正決定「會不會動到真的錢」的是前者。prod 是 live，那組路由根本不會存在，
    所以 /demo 回的是 404，不是一個「功能已停用」的頁面。
    綁在 live/sandbox 這個字上，哪天多開一個環境也不會錯。
    """
    from app.routers import (events, health, internal, orders, plans, push,
                             subscriptions, webhooks)
    target.include_router(health.router)
    target.include_router(orders.router)
    target.include_router(plans.router)
    target.include_router(subscriptions.router)
    target.include_router(events.router)
    target.include_router(webhooks.router)
    target.include_router(push.router)
    target.include_router(internal.router)

    if get_settings().paypal_env == "sandbox":
        from app.demo import routes as demo
        target.include_router(demo.router)


mount_routers(app)
```

（把原本檔案結尾的 `_mount()` 定義與 `_mount()` 呼叫整段換掉。）

建立 `app/demo/__init__.py`（空檔案）。

建立 `app/demo/flows.py`：

```python
"""demo 的動作。這一層不碰 FastAPI 的 Request/Response。

## 為什麼直接呼叫 router 函式

⚠️ **不要為了 demo 再實作一次建單邏輯。** 金額驗證、冪等（同 reference_id
回原本那筆）、狀態機、404 語意全部沿用 `/v1/*` 那一份。手抄一份的話，
那份平常不會被真流量執行 —— 而那是最糟的一種程式碼。
（同樣的理由見 app/event_view.py 的說明。）

## 身分

`caller_id = "demo"`。每張業務表都有 caller_id，所以 demo 的訂單、訂閱、
事件天生跟真 caller 隔離，而 `GET /v1/events` 的游標本來就不匹配別人的。

⚠️ scope 在這裡其實不會被檢查（檢查發生在 `require()` 這個 dependency 裡，
不在函式本體）。還是把完整的 scope 列出來，因為這個物件代表的是
「一個有這些權限的 caller」—— 寫成空集合會讓讀的人以為 scope 不重要。
"""
from app.auth import Caller

DEMO_CALLER_ID = "demo"

DEMO_CALLER = Caller(
    caller_id=DEMO_CALLER_ID,
    scopes=frozenset({
        "orders:read", "orders:write",
        "plans:read", "plans:write",
        "subscriptions:read", "subscriptions:write",
        "events:read",
        "webhooks:read", "webhooks:write",
    }),
)
```

建立 `app/demo/routes.py`：

```python
"""付款模擬頁的 HTTP 形狀。**只在 sandbox 掛得上**（見 app/main.py）。

它是給人操作的驗證工具，**不是 caller 的接入範例** —— 它就是服務本人，
沒有 API key、沒有跨服務的信任邊界。caller 要抄的東西在 README 的
〈怎麼接事件推送〉。
"""
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = logging.getLogger("demo")

router = APIRouter(prefix="/demo", tags=["demo"])

_PAGE = Path(__file__).with_name("page.html")


@router.get("", response_class=HTMLResponse)
def page() -> HTMLResponse:
    """單一靜態檔。刻意不引 Jinja2 —— 這個 repo 連 DB driver 都挑不用編譯的，
    為了一組 dev 用的頁面拉進樣板引擎不划算。動態部分由瀏覽器打
    /demo/api/* 拿 JSON。"""
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))
```

建立 `app/demo/page.html`（Task 5 會換成完整版本，先放最小的殼）：

```html
<!doctype html>
<meta charset="utf-8">
<title>payment-paypal 模擬頁</title>
<h1>payment-paypal 模擬頁</h1>
<p>建置中。</p>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 跑全部測試,確認沒有打壞既有的**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS（既有 178 + 新的 3）

- [ ] **Step 6: Commit**

```bash
git add app/demo tests/test_demo.py app/main.py
git commit -m "模擬頁的骨架與 prod 隔離：只在 sandbox 掛得上

掛載條件綁 paypal_env 不綁 app_env —— 真正決定「會不會動到真的錢」的是
前者。prod 是 live，那組路由根本不會註冊，所以 /demo 回 404。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 單筆付款動線

**Files:**
- Modify: `app/demo/flows.py`, `app/demo/routes.py`
- Test: `tests/test_demo.py`

**Interfaces:**
- Consumes: Task 1 的 `DEMO_CALLER`、`DEMO_CALLER_ID`
- Produces:
  - `flows.start_order(amount: str, base_url: str) -> dict` —— 回 `{"reference_id": str, "approve_url": str | None, "order": dict}`
  - `flows.finish_order(reference_id: str) -> str` —— 回結果字串 `"paid"` / `"missing"` / `"error:<訊息>"`
  - `routes` 上的 `POST /demo/api/orders`、`GET /demo/return/order/{reference_id}`、`GET /demo/cancel/order/{reference_id}`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_demo.py` 末尾加：

```python
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
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: FAIL —— `AttributeError: module 'app.demo.flows' has no attribute 'start_order'`

- [ ] **Step 3: 實作**

在 `app/demo/flows.py` 的 import 區塊加：

```python
import logging
import uuid

from fastapi import HTTPException

from app.models import OrderCreate
from app.routers import orders as orders_router
from app.store import orders as orders_store

log = logging.getLogger("demo")
```

並在檔案末尾加：

```python
# demo 的金額固定用 USD —— 帳號只支援這一種（見 SUPPORTED_CURRENCIES）。
DEMO_CURRENCY = "USD"


def _reference() -> str:
    """每次按下去都是一筆新的單。

    ⚠️ 刻意不重用 reference_id：`POST /v1/orders` 對同一個 reference_id 是
    冪等的（回原本那筆），重用的話第二次按下去會拿到第一筆的 approve_url，
    看起來像「按了沒反應」。
    """
    return f"demo-{uuid.uuid4().hex[:12]}"


def start_order(amount: str, base_url: str) -> dict:
    """建一筆單，回 approve_url 讓前端跳過去。

    ⚠️ 導回網址帶的是**我們自己產生的 reference_id**，不是 PayPal 的 token。
    我們建單前就知道它，放進網址是零成本；靠 token 反查要多一次查詢，
    而且訂單與訂閱兩條路 PayPal 帶回來的參數名不一樣（token / subscription_id），
    統一不了。
    """
    ref = _reference()
    body = OrderCreate(
        reference_id=ref,
        amount=amount,
        currency=DEMO_CURRENCY,
        description="payment-paypal demo 單筆付款",
        return_url=f"{base_url}/demo/return/order/{ref}",
        cancel_url=f"{base_url}/demo/cancel/order/{ref}",
    )
    out = orders_router.create_order(body, caller=DEMO_CALLER)
    return {"reference_id": ref, "approve_url": out.get("approve_url"),
            "order": out}


def finish_order(reference_id: str) -> str:
    """使用者從 PayPal 導回來之後 capture。回一個給網址用的結果字串。

    ⚠️ 這裡吞掉例外並回字串，不讓它變成 500 —— 使用者是**在瀏覽器裡**，
    給他一個 stack trace 沒有任何意義。真正的錯誤進 log。
    """
    row = orders_store.get_by_reference(DEMO_CALLER_ID, reference_id)
    if not row:
        return "missing"
    try:
        orders_router.capture(str(row["id"]), caller=DEMO_CALLER)
    except HTTPException as exc:
        log.error("demo capture 失敗 ref=%s：%s", reference_id, exc.detail)
        return "error"
    except Exception as exc:                    # noqa: BLE001
        log.error("demo capture 爆了 ref=%s：%s: %s",
                  reference_id, type(exc).__name__, exc)
        return "error"
    return "paid"
```

在 `app/demo/routes.py` 的 import 加：

```python
from fastapi import Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.demo import flows
from app.urls import base_url
```

並在檔案末尾加：

```python
class _AmountIn(BaseModel):
    amount: str = Field(default="9.99")


@router.post("/api/orders")
def api_create_order(body: _AmountIn, request: Request):
    return flows.start_order(body.amount, base_url(request))


@router.get("/return/order/{reference_id}")
def order_return(reference_id: str):
    """PayPal 把**使用者的瀏覽器**導回這裡。導回一律回 303 到 /demo，
    讓網址列乾淨、重新整理也不會再 capture 一次。"""
    result = flows.finish_order(reference_id)
    return RedirectResponse(f"/demo?ref={reference_id}&result={result}", 303)


@router.get("/cancel/order/{reference_id}")
def order_cancel(reference_id: str):
    return RedirectResponse(f"/demo?ref={reference_id}&result=cancelled", 303)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: Commit**

```bash
git add app/demo tests/test_demo.py
git commit -m "模擬頁：單筆付款動線

建單→跳 PayPal→導回→capture。導回網址帶我們自己產生的 reference_id，
不靠 PayPal 的 token 反查（訂單與訂閱回傳的參數名不一樣，統一不了）。

金額驗證、冪等、狀態機全部沿用 /v1 那一份 —— demo 沒有自己再寫一次。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 訂閱動線與方案重用

**Files:**
- Modify: `app/demo/flows.py`, `app/demo/routes.py`
- Test: `tests/test_demo.py`

**Interfaces:**
- Consumes: Task 2 的 `flows._reference()`、`DEMO_CALLER`
- Produces:
  - `flows.ensure_plan() -> dict` —— 回方案（含 `id`、`name`），已存在就重用
  - `flows.start_subscription(base_url: str) -> dict` —— 回 `{"reference_id", "approve_url", "subscription"}`
  - `routes` 上的 `POST /demo/api/subscriptions`、`GET /demo/return/subscription/{reference_id}`、`GET /demo/cancel/subscription/{reference_id}`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_demo.py` 末尾加：

```python
@pytest.fixture
def subs_env(monkeypatch):
    """方案 + 訂閱兩條 store，加上假的 PayPal。"""
    from app.routers import plans as plans_router
    from app.routers import subscriptions as subs_router

    plans, subs = {}, {}
    calls = {"products": 0, "plans": 0, "subs": []}

    # --- plans
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
    monkeypatch.setattr(subs_router.plans_store, "list_", plan_list)

    # --- subscriptions
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
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: FAIL —— `AttributeError: module 'app.demo.flows' has no attribute 'ensure_plan'`

- [ ] **Step 3: 實作**

在 `app/demo/flows.py` 的 import 區塊補上：

```python
from app.models import PlanCreate, SubscriptionCreate
from app.routers import plans as plans_router
from app.routers import subscriptions as subs_router
from app.store import plans as plans_store
```

並在檔案末尾加：

```python
DEMO_PLAN_NAME = "payment-paypal demo 月訂閱"
DEMO_PLAN_AMOUNT = "10.00"


def ensure_plan() -> dict:
    """回 demo 用的方案，沒有就建一個。

    ⚠️ 一定要重用。建方案在 PayPal 那邊是 product + plan 兩個永久物件，
    每按一次訂閱就多一組的話，後台很快就被塞滿而且分不出哪個在用。
    用名字比對就夠了 —— 這是 demo，不需要一個 `is_demo` 欄位。
    """
    for row in plans_store.list_(DEMO_CALLER_ID, limit=200):
        if row["name"] == DEMO_PLAN_NAME and row["status"] == "ACTIVE":
            return {**row, "id": str(row["id"])}
    return plans_router.create_plan(
        PlanCreate(name=DEMO_PLAN_NAME, amount=DEMO_PLAN_AMOUNT,
                   currency=DEMO_CURRENCY, interval_count=1,
                   description="payment-paypal 模擬頁用的月訂閱方案"),
        caller=DEMO_CALLER)


def start_subscription(base_url: str) -> dict:
    """建一筆訂閱，回 approve_url。

    ⚠️ 訂閱**沒有 capture**。使用者在 PayPal 按下訂閱之後，本地狀態要等
    webhook `BILLING.SUBSCRIPTION.ACTIVATED` 才會變成 ACTIVE ——
    所以導回頁只能說「已送出」，真正的確認由推送那條路帶回來。
    """
    plan = ensure_plan()
    ref = _reference()
    body = SubscriptionCreate(
        reference_id=ref,
        plan_id=str(plan["id"]),
        return_url=f"{base_url}/demo/return/subscription/{ref}",
        cancel_url=f"{base_url}/demo/cancel/subscription/{ref}",
    )
    out = subs_router.create_subscription(body, caller=DEMO_CALLER)
    return {"reference_id": ref, "approve_url": out.get("approve_url"),
            "subscription": out}
```

在 `app/demo/routes.py` 末尾加：

```python
@router.post("/api/subscriptions")
def api_create_subscription(request: Request):
    return flows.start_subscription(base_url(request))


@router.get("/return/subscription/{reference_id}")
def subscription_return(reference_id: str):
    """⚠️ 這裡**不做任何事**，只導回。訂閱轉 ACTIVE 是 webhook 的工作 ——
    在這裡搶著去 PayPal 問一次只會在導回路徑上多一次外部呼叫，而導回要快。"""
    return RedirectResponse(f"/demo?ref={reference_id}&result=subscribed", 303)


@router.get("/cancel/subscription/{reference_id}")
def subscription_cancel(reference_id: str):
    return RedirectResponse(f"/demo?ref={reference_id}&result=cancelled", 303)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: PASS（13 passed）

- [ ] **Step 5: Commit**

```bash
git add app/demo tests/test_demo.py
git commit -m "模擬頁：訂閱動線，方案會重用不會每次都建新的

訂閱沒有 capture —— 它靠 webhook BILLING.SUBSCRIPTION.ACTIVATED 轉 ACTIVE，
所以導回頁只說「已送出」，真正的確認由推送帶回來。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 推送 sink 與啟用推送

**Files:**
- Modify: `app/demo/flows.py`, `app/demo/routes.py`
- Test: `tests/test_demo.py`

**Interfaces:**
- Consumes: Task 1 的 `DEMO_CALLER`；`app.webhooks.signing.secret_for/header/TOLERANCE_SECONDS`
- Produces:
  - `flows.enable_push(base_url: str) -> dict` —— 回 `PUT /v1/webhook-endpoint` 的回應
  - `flows.verify_push(raw: bytes, header: str | None) -> bool`
  - `routes` 上的 `POST /demo/api/push/enable`、`POST /demo/sink`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_demo.py` 末尾加：

```python
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
    # 用「重新序列化後」的內容去簽，但送出「原文」—— 必須被擋掉
    r = client.post("/demo/sink", content=original,
                    headers={"X-Signature": _signed(reserialized)})
    assert r.status_code == 401
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: FAIL —— `AttributeError: module 'app.demo.flows' has no attribute 'enable_push'`

- [ ] **Step 3: 實作**

在 `app/demo/flows.py` 的 import 區塊補上：

```python
import hmac

from app.models import WebhookEndpointPut
from app.routers import push as push_router
from app.webhooks import signing
```

並在檔案末尾加：

```python
def enable_push(base_url: str) -> dict:
    """把 caller `demo` 的推送端點指向服務自己的 /demo/sink。

    ⚠️ 這是刻意做成畫面上**可見的一步**，因為那正是 caller 要做的事
    （`PUT /v1/webhook-endpoint`）。藏起來自動做掉的話，這個 demo 就少演了
    最重要的一段。

    ⚠️ 本機跑（http://localhost）會被 targets.validate() 擋下來回 400 ——
    推送端點只收 https。那是對的行為，不要為了 demo 放寬它。
    """
    return push_router.put_endpoint(
        WebhookEndpointPut(url=f"{base_url}/demo/sink"), caller=DEMO_CALLER)


def verify_push(raw: bytes, header) -> bool:
    """照 README 給 caller 的規則驗簽。**這裡是一份可執行的示範。**

    ⚠️ 驗的是 raw bytes，不是重新序列化的 JSON。重新 json.dumps 出來的字串
    跟原文不保證逐位元組相同（鍵的順序、Unicode 跳脫、空白都可能不同），
    而且只在有非 ASCII 的 payload 上才發作。
    """
    parts = {}
    for kv in (header or "").split(","):
        i = kv.find("=")
        if i > 0:
            parts[kv[:i].strip()] = kv[i + 1:]
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - t) > signing.TOLERANCE_SECONDS:
        return False        # 防重放
    try:
        expected = signing.signature(signing.secret_for(DEMO_CALLER_ID), t, raw)
    except RuntimeError:
        return False        # 推送未設定，算不出密鑰
    return hmac.compare_digest(expected, parts.get("v1", ""))
```

同時在 `flows.py` 的 import 區塊補 `import time`。

在 `app/demo/routes.py` 的 import 補 `from fastapi import HTTPException`，並在末尾加：

```python
@router.post("/api/push/enable")
def api_enable_push(request: Request):
    return flows.enable_push(base_url(request))


@router.post("/sink")
async def sink(request: Request):
    """推送的接收端。**它做的事跟一個真 caller 一模一樣。**

    ⚠️ 這支是 async 的，因為驗簽必須拿到 `await request.body()` 的原始 bytes。
    但它裡面**沒有任何 I/O** —— 只有 HMAC 計算，所以不會卡住事件迴圈。
    不要在這裡加 DB 查詢或對外 HTTP（見 app/routers/webhooks.py 開頭的說明）。

    ⚠️ 它也**不存任何狀態**。Cloud Run 有多個實例，推送打到哪一台不確定，
    存記憶體的話畫面會變成「有時看得到有時看不到」。畫面一律從 DB 讀。
    """
    raw = await request.body()
    if not flows.verify_push(raw, request.headers.get("x-signature")):
        raise HTTPException(status_code=401,
                            detail="signature verification failed")
    log.info("demo sink 收到推送 event=%s delivery=%s attempt=%s",
             request.headers.get("x-event-id"),
             request.headers.get("x-delivery-id"),
             request.headers.get("x-delivery-attempt"))
    return {"received": True}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: PASS（19 passed）

- [ ] **Step 5: Commit**

```bash
git add app/demo tests/test_demo.py
git commit -m "模擬頁：推送 sink，而且它真的驗簽

sink 做的事跟一個真 caller 一模一樣 —— 驗原始 bytes、擋過期時間戳、
constant-time 比對。它是一份可執行的示範。

sink 不存任何狀態：多實例下存記憶體會變成「有時看得到」。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 狀態彙整與完整頁面

**Files:**
- Modify: `app/demo/flows.py`, `app/demo/routes.py`, `app/demo/page.html`
- Test: `tests/test_demo.py`

**Interfaces:**
- Consumes: 前面所有 flows
- Produces:
  - `flows.state() -> dict` —— `{"orders", "subscriptions", "events", "deliveries", "push_configured", "push_endpoint"}`
  - `routes` 上的 `GET /demo/api/state`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_demo.py` 末尾加：

```python
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
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: FAIL —— `AttributeError: module 'app.demo.flows' has no attribute 'state'`

- [ ] **Step 3: 實作**

在 `app/demo/flows.py` 的 import 區塊補上：

```python
from app.config import get_settings
from app.routers import events as events_router
from app.store import webhook_endpoints as endpoints_store
```

並在檔案末尾加：

```python
# 每一段各自包一層，讓 state() 的任何一塊壞掉都不會拖垮整個畫面 ——
# /demo 壞掉的時候，正是最需要它回答問題的時候。

def _orders():
    return orders_router.list_orders(caller=DEMO_CALLER)["items"]


def _subscriptions():
    return subs_router.list_subscriptions(caller=DEMO_CALLER)["items"]


def _events():
    return events_router.list_events(after=0, limit=50, caller=DEMO_CALLER)["items"]


def _deliveries():
    if not get_settings().push_configured:
        return []
    return push_router.list_deliveries(caller=DEMO_CALLER)["items"]


def _safe(fn, what):
    try:
        return fn()
    except Exception as exc:                    # noqa: BLE001 — 畫面要活著
        log.error("demo state 的 %s 讀不到：%s: %s",
                  what, type(exc).__name__, exc)
        return []


def state() -> dict:
    """給頁面輪詢的一包資料，全部從 DB 讀。

    ⚠️ **不從 sink 的記憶體讀。** Cloud Run 有多個實例，推送打到哪一台不確定 ——
    存記憶體在單實例的本機測試會過，上了 dev 之後變成「有時候看得到」，
    是最難查的那一種。

    `deliveries` 是這個畫面真正的價值：它說得出「那筆事件推出去了沒有、
    試了幾次、對方回什麼」。
    """
    s = get_settings()
    endpoint = None
    if s.push_configured:
        row = _safe(lambda: endpoints_store.get(DEMO_CALLER_ID), "endpoint")
        endpoint = row if isinstance(row, dict) else None
    return {
        "orders": _safe(_orders, "orders"),
        "subscriptions": _safe(_subscriptions, "subscriptions"),
        "events": _safe(_events, "events"),
        "deliveries": _safe(_deliveries, "deliveries"),
        "push_configured": s.push_configured,
        "push_endpoint": {"url": endpoint["url"], "active": endpoint["active"]}
                          if endpoint else None,
    }
```

在 `app/demo/routes.py` 末尾加：

```python
@router.get("/api/state")
def api_state():
    return flows.state()
```

把 `app/demo/page.html` 換成完整版本：

```html
<!doctype html>
<html lang="zh-Hant">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>payment-paypal 模擬頁</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #fff; --line: #e3e6ea; --ink: #1a1d21;
    --muted: #6b7280; --ok: #0f7b3d; --warn: #a15c00; --bad: #b3261e;
    --accent: #0057d9;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14171a; --card:#1c2024; --line:#2c3238; --ink:#e6e8ea;
            --muted:#9aa3ad; --ok:#54c98a; --warn:#e0a33e; --bad:#f2857d;
            --accent:#6aa6ff; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
         font:15px/1.6 -apple-system,"Noto Sans TC",sans-serif; }
  .wrap { max-width: 960px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 20px; font-size: 13px; }
  .cards { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
  .card h2 { font-size:15px; margin:0 0 10px; }
  label { display:block; font-size:13px; color:var(--muted); margin-bottom:4px; }
  input { width:100%; padding:8px 10px; border:1px solid var(--line);
          border-radius:6px; background:var(--bg); color:var(--ink); font-size:15px; }
  button { margin-top:10px; width:100%; padding:9px 12px; border:0; border-radius:6px;
           background:var(--accent); color:#fff; font-size:14px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  button.ghost { background:transparent; color:var(--accent); border:1px solid var(--line); }
  .note { font-size:12px; color:var(--muted); margin-top:8px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
          white-space:nowrap; }
  th { color:var(--muted); font-weight:600; }
  .scroll { overflow-x:auto; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; }
  .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
  .banner { padding:10px 14px; border-radius:8px; margin-bottom:16px; font-size:14px;
            border:1px solid var(--line); background:var(--card); }
  section { margin-top:24px; }
</style>
<div class="wrap">
  <h1>payment-paypal 模擬頁</h1>
  <p class="sub">只在 sandbox 環境存在。這裡花的是假錢,但走的是真的 PayPal、真的佇列、真的簽章。</p>

  <div id="banner" class="banner" hidden></div>

  <div class="cards">
    <div class="card">
      <h2>單筆付款</h2>
      <label for="amount">金額(USD)</label>
      <input id="amount" value="9.99" inputmode="decimal">
      <button id="pay">建立訂單並前往 PayPal</button>
      <p class="note">回來之後會自動 capture,錢才算真的收到。</p>
    </div>

    <div class="card">
      <h2>訂閱付款</h2>
      <p class="note" style="margin-top:0">方案:每月 USD 10.00。第一次會自動建立,之後重用同一個。</p>
      <button id="sub">建立訂閱並前往 PayPal</button>
      <p class="note">訂閱<strong>沒有 capture</strong> —— 它靠 webhook
        <code>BILLING.SUBSCRIPTION.ACTIVATED</code> 轉成 ACTIVE,所以回來之後
        要等一下才會看到狀態變化。</p>
    </div>

    <div class="card">
      <h2>事件推送</h2>
      <p class="note" style="margin-top:0" id="pushstate">讀取中…</p>
      <button id="push" class="ghost">啟用推送(註冊端點)</button>
      <p class="note">註冊之後,事件落地就會主動推到本服務的
        <code>/demo/sink</code>,而 sink 會<strong>真的驗簽</strong>。</p>
    </div>
  </div>

  <section>
    <h2 style="font-size:15px">投遞紀錄 —— 「那筆推出去了沒有」</h2>
    <div class="card scroll"><table id="deliveries"><tbody></tbody></table></div>
  </section>

  <section>
    <h2 style="font-size:15px">事件</h2>
    <div class="card scroll"><table id="events"><tbody></tbody></table></div>
  </section>

  <section>
    <h2 style="font-size:15px">訂單</h2>
    <div class="card scroll"><table id="orders"><tbody></tbody></table></div>
  </section>

  <section>
    <h2 style="font-size:15px">訂閱</h2>
    <div class="card scroll"><table id="subscriptions"><tbody></tbody></table></div>
  </section>
</div>

<script>
const $ = (id) => document.getElementById(id);

const RESULT_TEXT = {
  paid: ["ok", "付款完成,已經 capture。"],
  subscribed: ["ok", "訂閱已送出。狀態要等 PayPal 的 webhook 才會變 ACTIVE,稍等一下。"],
  cancelled: ["warn", "你在 PayPal 取消了。"],
  missing: ["bad", "找不到那筆單 —— 導回的網址對不上任何紀錄。"],
  error: ["bad", "回來之後處理失敗,詳情看服務的 log。"],
};

function showBanner() {
  const p = new URLSearchParams(location.search);
  const r = p.get("result");
  if (!r) return;
  const [cls, text] = RESULT_TEXT[r] || ["warn", r];
  const b = $("banner");
  b.hidden = false;
  b.innerHTML = `<span class="${cls}">●</span> ${text}` +
    (p.get("ref") ? ` <span class="note">(${p.get("ref")})</span>` : "");
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(
    typeof data.detail === "string" ? data.detail
      : (data.detail && data.detail.message) || `HTTP ${r.status}`);
  return data;
}

function go(btn, fn) {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const out = await fn();
      if (out && out.approve_url) { location.href = out.approve_url; return; }
      await refresh();
    } catch (e) {
      const b = $("banner");
      b.hidden = false;
      b.innerHTML = `<span class="bad">●</span> ${e.message}`;
    } finally { btn.disabled = false; }
  });
}

go($("pay"), () => post("/demo/api/orders", {amount: $("amount").value}));
go($("sub"), () => post("/demo/api/subscriptions"));
go($("push"), () => post("/demo/api/push/enable"));

function table(el, cols, rows, empty) {
  const body = el.querySelector("tbody");
  if (!rows.length) {
    body.innerHTML = `<tr><td class="note">${empty}</td></tr>`;
    return;
  }
  body.innerHTML =
    `<tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}</tr>` +
    rows.map(r => `<tr>${cols.map(c => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("");
}

const esc = (v) => String(v == null ? "—" : v)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;");

const statusClass = (s) =>
  ({delivered: "ok", COMPLETED: "ok", ACTIVE: "ok",
    failed: "warn", dead: "bad", DENIED: "bad"}[s] || "");

async function refresh() {
  let s;
  try { s = await (await fetch("/demo/api/state")).json(); }
  catch { return; }

  $("pushstate").innerHTML = !s.push_configured
    ? '<span class="bad">●</span> 服務端沒設定推送(缺 WEBHOOK_SIGNING_KEY / INTERNAL_KEY)'
    : s.push_endpoint
      ? `<span class="ok">●</span> 已註冊:<code>${esc(s.push_endpoint.url)}</code>`
      : '<span class="warn">●</span> 尚未註冊端點';

  table($("deliveries"), [
    ["事件", d => esc(d.event_id === null ? "ping" : d.event_id)],
    ["狀態", d => `<span class="${statusClass(d.status)}">${esc(d.status)}</span>`],
    ["嘗試", d => esc(d.attempts)],
    ["對方回", d => esc(d.last_status)],
    ["送去哪", d => `<code>${esc(d.url)}</code>`],
  ], s.deliveries, "還沒有任何投遞。啟用推送之後付一筆看看。");

  table($("events"), [
    ["id", e => esc(e.id)],
    ["類型", e => esc(e.event_type)],
    ["對象", e => esc(e.subject_kind)],
    ["時間", e => esc(e.received_at)],
  ], s.events, "還沒有事件。");

  table($("orders"), [
    ["reference", o => esc(o.reference_id)],
    ["金額", o => `${esc(o.amount)} ${esc(o.currency)}`],
    ["狀態", o => `<span class="${statusClass(o.status)}">${esc(o.status)}</span>`],
  ], s.orders, "還沒有訂單。");

  table($("subscriptions"), [
    ["reference", x => esc(x.reference_id)],
    ["狀態", x => `<span class="${statusClass(x.status)}">${esc(x.status)}</span>`],
    ["PayPal id", x => esc(x.paypal_subscription_id)],
  ], s.subscriptions, "還沒有訂閱。");
}

showBanner();
refresh();
setInterval(refresh, 3000);
</script>
</html>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_demo.py -q`
Expected: PASS（22 passed）

- [ ] **Step 5: 跑全部測試**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS（既有 178 + demo 22 = 200）

- [ ] **Step 6: Commit**

```bash
git add app/demo tests/test_demo.py
git commit -m "模擬頁：狀態彙整與完整畫面

畫面靠輪詢 /demo/api/state,而那一包全部從 DB 讀 —— 不從 sink 的記憶體讀,
多實例下那會變成「有時候看得到」。

投遞紀錄是這個畫面真正的價值:它說得出「那筆事件推出去了沒有、試了幾次、
對方回什麼」。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: README 與部署驗證

**Files:**
- Modify: `README.md`
- Test: 實際部署到 dev 並用瀏覽器走一次

**Interfaces:**
- Consumes: 前面所有 task
- Produces: 無程式介面；交付物是「dev 上真的跑得起來」

- [ ] **Step 1: 在 README 加一節**

在 `## 本機開發` 之前插入：

````markdown
## 模擬頁（只有 sandbox 有）

`GET /demo` —— 把單筆付款與訂閱付款從頭到尾走一次,順便看得到事件推送的結果。

```
https://payment-paypal-<hash>-de.a.run.app/demo
```

**prod 沒有這組路由。** `app/main.py` 只在 `paypal_env == "sandbox"` 時掛上它 ——
判斷條件綁 `paypal_env` 不綁 `APP_ENV`,因為真正決定「會不會動到真的錢」的是前者。
prod 是 `live`,`/demo` 回 404。

### 用之前要先有一個 sandbox 買家帳號

developer.paypal.com → Testing Tools → **Sandbox Accounts** → 挑一個
**Personal** 類型的帳號,記下 email 與密碼(密碼可以在那頁重設)。
跳到 PayPal 之後就是用它登入付款。**沒有它,整條路會停在 approve 頁。**

### 它證明什麼、不證明什麼

✅ 證明:建單 → PayPal → 導回 → capture 是通的;事件會落地;推送會被排進
Cloud Tasks、打到端點、而且端點驗得過簽章。

❌ **不是 caller 的接入範例。** 它就是服務本人 —— 沒有 API key、
沒有跨服務的信任邊界。caller 要抄的東西在〈怎麼接事件推送〉那一節。

### demo 的資料不會混進真 caller

所有 demo 資料的 `caller_id` 都是 `demo`,而每張業務表都有這個欄位、
`GET /v1/events` 的游標本來就不匹配別人的。要清掉就是刪 `caller_id = 'demo'` 的列。

⚠️ demo 路由**不驗 API key**。dev 服務是公開的(PayPal 的 webhook 必須打得到),
所以任何知道網址的人都能建 sandbox 訂單。判斷是可接受:假錢、dev DB、資料隔離。
````

- [ ] **Step 2: 跑全部測試最後確認**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS（200）

- [ ] **Step 3: Commit 並推上去（push main 會部署到 dev）**

```bash
git add README.md
git commit -m "README：模擬頁的用法與前置條件

寫清楚它證明什麼(整條路是通的)、不證明什麼(它不是 caller 的接入範例)。
以及 sandbox 買家帳號這個前置條件 —— 沒有它整條路會停在 approve 頁。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 4: 等 CI 部署完成**

Run: `gh run watch $(gh run list --limit 1 --json databaseId -q '.[0].databaseId') --exit-status`
Expected: 綠燈。

⚠️ 上次觀察到 GitHub 建立 run 會延遲十幾分鐘。`gh run list` 看不到新的 run 時
先等,不要急著用 `workflow_dispatch` 補一次(會變成兩次部署)。

- [ ] **Step 5: 驗證 dev 上的 demo 頁**

```bash
BASE=https://payment-paypal-2wcfrjmezq-de.a.run.app
curl -s -o /dev/null -w "GET /demo -> %{http_code}\n" "$BASE/demo"          # 期望 200
curl -s "$BASE/demo/api/state" | python3 -m json.tool | head -20            # 期望有五個鍵
curl -s -o /dev/null -w "sink 無簽章 -> %{http_code}\n" \
  -X POST "$BASE/demo/sink" -d '{}'                                         # 期望 401
```

- [ ] **Step 6: 用瀏覽器實際走一次**

1. 開 `$BASE/demo`
2. 按「啟用推送」→ 畫面上的推送狀態要變成「已註冊」
3. 按「建立訂單並前往 PayPal」→ 用 sandbox 買家帳號付款 → 導回後 banner 說「付款完成」
4. 等幾秒 → **投遞紀錄**出現一列 `delivered`,事件表出現 `PAYMENT.CAPTURE.COMPLETED`
5. 按「建立訂閱並前往 PayPal」→ 訂閱 → 導回 → 等 webhook → 訂閱狀態變 `ACTIVE`

---

## Self-Review

**Spec coverage:**

| Spec 段落 | 對應 task |
|---|---|
| 架構 / 三個檔案 / 不加相依 | Task 1 |
| 直接呼叫 router 函式 | Task 1（`DEMO_CALLER`）+ Task 2/3 使用 |
| prod 隔離（`paypal_env`） | Task 1 |
| 單筆動線、導回帶自己的 id | Task 2 |
| 訂閱動線、方案重用、不 capture | Task 3 |
| 啟用推送、sink 驗簽 | Task 4 |
| sink 不存狀態、state 從 DB 讀 | Task 5 |
| 安全邊界（不驗 key、sink 驗簽） | Task 4 + Task 6（README 寫明） |
| 測試要求（全部 9 條） | 分散在 Task 1–5,全部有對應測試 |
| 前置條件（sandbox 買家帳號） | Task 6 |

**Placeholder scan:** 無 TBD／TODO；每個 code step 都有可直接貼上的完整程式碼。

**Type consistency:**
- `flows.start_order` / `start_subscription` 都回 `{"reference_id", "approve_url", ...}` —— 頁面的 `go()` 統一讀 `out.approve_url`，一致。
- `flows.finish_order` 回字串，路由把它放進 `result=` 查詢參數，`RESULT_TEXT` 的鍵涵蓋 `paid`/`missing`/`error`/`cancelled`/`subscribed`，一致。
- `_probe_app(fake_settings, env)` 在 Task 1 定義，Task 2–5 沿用同一個簽名。
- `state()` 的鍵與 `page.html` 讀的鍵（`push_configured`、`push_endpoint`、`deliveries`、`events`、`orders`、`subscriptions`）逐一對得上。
