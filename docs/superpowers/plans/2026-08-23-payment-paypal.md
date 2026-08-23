# payment-paypal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建一支以 API key 認證 caller 的 PayPal 底層後端，支援一次性訂單與月訂閱，部署到 Cloud Run dev 後以 sandbox 實測，通過即打 tag 上 prod。

**Architecture:** FastAPI 單一服務，三層切分 —— `paypal/` 只跟 PayPal 講話、`store/` 只跟 Postgres 講話、`routers/` 把兩者接起來。狀態變化由 PayPal webhook 推進本地表，caller 以游標增量拉 `/v1/events` 取得事件。所有業務表帶 `caller_id`，隔離在 `store/` 層強制。

**Tech Stack:** Python 3.12（`python:3.12-slim`）、FastAPI、pydantic v2、httpx（同步）、pg8000、Cloud SQL Postgres 17（IAM 認證）、Cloud Run、adamsourceinfo-org/ci

**Spec:** `docs/superpowers/specs/2026-08-23-payment-paypal-design.md`

## Global Constraints

- **幣別只支援 USD**（帳號限制）。`SUPPORTED_CURRENCIES=USD`，pydantic 進門驗證，不在清單回 400。小數位數是幣別屬性（`{"USD": 2}`），不寫死 2。
- **幣別沒有預設值**，caller 必須明確指定。
- **服務名 `payment-paypal`**，repo `adamsourceinfo-org/payment-paypal`，Cloud Run service 同名。
- **webhook 路徑 `/v1/webhooks`**，不驗 API key，驗 PayPal 簽章，必須用 `await request.body()` 的**原始 bytes**。
- **PayPal base URL 由 `PAYPAL_ENV` 推導**：`sandbox` → `https://api-m.sandbox.paypal.com`，`live` → `https://api-m.paypal.com`。程式裡不得出現寫死的 PayPal 網址。
- **IAM DB token 必須在連線池的「建立新連線」工廠取**，依 `expires_in` 快取、剩餘 < 60 秒才重取。不可在啟動時取一次就永久使用。
- **`store/` 的每個查詢強制 `WHERE caller_id = :me`**。`events.caller_id` 可為 NULL，NULL 對每個 caller 都不可見。
- **不存 API key 明文**，只存 `sha256(key)`。
- **不做 admin API、不存在 bootstrap admin key**。API key 由 `scripts/add-caller.sh` 手動建立。
- **任何情況不把 client secret / access token / API key 明文寫進日誌。**
- **CI 不跑測試**，測試在本機驗過才推。
- 錯誤語意：無效 key → 401（不區分原因）；scope 不足 → 403；查別人的資源 → 404（不是 403）；重複 `reference_id` → 200 + 原本那筆。

---

## File Structure

| 檔案 | 責任 |
|---|---|
| `Dockerfile` | `python:3.12-slim`，監聽 `$PORT` |
| `requirements.txt` | 釘版本 |
| `app/config.py` | **唯一**碰 `os.environ` 的地方。啟動時讀一次驗一次 |
| `app/main.py` | FastAPI app、router 掛載、lifespan 跑 migration |
| `app/db.py` | IAM token 工廠、連線池、migration runner |
| `app/auth.py` | API key 驗證 + scope 檢查（FastAPI dependency） |
| `app/errors.py` | 錯誤型別與 exception handler |
| `app/money.py` | 幣別驗證與小數位數規則 |
| `app/models.py` | pydantic request/response schema |
| `app/paypal/client.py` | OAuth token 快取、HTTP、PayPal 錯誤轉譯 |
| `app/paypal/orders.py` | Orders v2 |
| `app/paypal/plans.py` | Catalog Products + Billing Plans |
| `app/paypal/subscriptions.py` | Subscriptions v1 |
| `app/paypal/webhooks.py` | 簽章驗證 |
| `app/store/*.py` | `api_keys` / `orders` / `plans` / `subscriptions` / `events`，SQL 只在這層 |
| `app/routers/*.py` | `health` / `orders` / `plans` / `subscriptions` / `events` / `webhooks` |
| `migrations/001_init.sql` | 全部建表 |
| `scripts/add-caller.sh` | 產 key、算 hash、INSERT |
| `.cicd/*` | CI 契約設定 |
| `.github/workflows/deploy.yml` | ci 的 caller stub，照抄不改 |
| `tests/*` | pytest |

---

### Task 1: 骨架、設定驗證、可部署的 /healthz

**Files:**
- Create: `Dockerfile`, `requirements.txt`, `app/__init__.py`, `app/config.py`, `app/main.py`, `app/routers/__init__.py`, `app/routers/health.py`, `.cicd/config.yml`, `.cicd/env.common`, `.cicd/env.dev`, `.cicd/env.prod`, `.cicd/secrets.dev`, `.cicd/secrets.prod`, `.github/workflows/deploy.yml`, `.gitignore`, `pytest.ini`
- Test: `tests/test_config.py`, `tests/test_health.py`

**Interfaces:**
- Produces: `app.config.Settings`（dataclass，欄位 `app_env, app_version, paypal_env, paypal_client_id, paypal_client_secret, paypal_webhook_id(Optional), paypal_timeout_seconds, db_pool_max, supported_currencies(frozenset), log_level, db_instance(Optional), db_user(Optional), db_name(Optional)`）、`app.config.load_settings() -> Settings`、`app.config.settings`（模組級單例）、`app.config.PAYPAL_API_BASE: dict[str,str]`
- Produces: `app.main.app`（FastAPI 實例）

**必要變數缺席 → `load_settings()` 丟 `RuntimeError`。`PAYPAL_WEBHOOK_ID` 缺席是允許的，值為 `None`。**

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_config.py
import pytest
from app.config import load_settings, PAYPAL_API_BASE

BASE = {
    "PAYPAL_ENV": "sandbox",
    "PAYPAL_CLIENT_ID": "cid",
    "PAYPAL_CLIENT_SECRET": "csecret",
}

def test_loads_required(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.paypal_env == "sandbox"
    assert s.paypal_client_id == "cid"
    assert s.supported_currencies == frozenset({"USD"})
    assert s.paypal_webhook_id is None          # 允許缺席

def test_missing_required_raises(monkeypatch):
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError) as e:
        load_settings()
    assert "PAYPAL_CLIENT_ID" in str(e.value)

def test_bad_paypal_env_raises(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PAYPAL_ENV", "production")
    with pytest.raises(RuntimeError):
        load_settings()

def test_api_base_is_derived():
    assert PAYPAL_API_BASE["sandbox"] == "https://api-m.sandbox.paypal.com"
    assert PAYPAL_API_BASE["live"] == "https://api-m.paypal.com"

def test_currencies_parsed(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SUPPORTED_CURRENCIES", "USD, EUR")
    assert load_settings().supported_currencies == frozenset({"USD", "EUR"})
```

```python
# tests/test_health.py
def test_healthz_reports_env_and_webhook_state(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "payment-paypal"
    assert body["paypal"]["env"] == "sandbox"
    assert body["paypal"]["webhook"] == "unconfigured"

def test_healthz_never_leaks_credentials(client):
    raw = client.get("/healthz").text
    assert "csecret" not in raw
    assert "cid" not in raw
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_config.py tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: 實作 config.py**

```python
# app/config.py
import os
from dataclasses import dataclass
from typing import Optional

PAYPAL_API_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

@dataclass(frozen=True)
class Settings:
    app_env: str
    app_version: str
    paypal_env: str
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: Optional[str]
    paypal_timeout_seconds: float
    db_pool_max: int
    supported_currencies: frozenset
    log_level: str
    db_instance: Optional[str]
    db_user: Optional[str]
    db_name: Optional[str]

    @property
    def paypal_api_base(self) -> str:
        return PAYPAL_API_BASE[self.paypal_env]

def _required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"缺少必要環境變數 {name}")
    return v

def load_settings() -> Settings:
    paypal_env = _required("PAYPAL_ENV")
    if paypal_env not in PAYPAL_API_BASE:
        raise RuntimeError(
            f"PAYPAL_ENV 只能是 {sorted(PAYPAL_API_BASE)}，收到 {paypal_env!r}")
    currencies = frozenset(
        c.strip().upper()
        for c in os.environ.get("SUPPORTED_CURRENCIES", "USD").split(",")
        if c.strip())
    if not currencies:
        raise RuntimeError("SUPPORTED_CURRENCIES 不能是空的")
    return Settings(
        app_env=os.environ.get("APP_ENV", "unknown"),
        app_version=os.environ.get("APP_VERSION", "(dev build)"),
        paypal_env=paypal_env,
        paypal_client_id=_required("PAYPAL_CLIENT_ID"),
        paypal_client_secret=_required("PAYPAL_CLIENT_SECRET"),
        paypal_webhook_id=os.environ.get("PAYPAL_WEBHOOK_ID") or None,
        paypal_timeout_seconds=float(os.environ.get("PAYPAL_TIMEOUT_SECONDS", "10")),
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "3")),
        supported_currencies=currencies,
        log_level=os.environ.get("LOG_LEVEL", "info"),
        db_instance=os.environ.get("INSTANCE_CONNECTION_NAME") or None,
        db_user=os.environ.get("DB_USER") or None,
        db_name=os.environ.get("DB_NAME") or None,
    )
```

`app/main.py` 建立 app、掛 health router、設 logging。`app/routers/health.py` 回傳 spec 的健康檢查 JSON（Task 2、4 會分別補上 `db` 與 `paypal.token` 區塊）。

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: 寫 .cicd 與 stub**

`.cicd/config.yml`、`env.common`、`env.dev`、`env.prod`、`secrets.dev`、`secrets.prod` 內容逐字照 spec「設定與機密 → 檔案內容」。
`.github/workflows/deploy.yml` 從 `~/repository/adamsourceinfo/ci/README.md` 的「開一個新專案」段落**照抄不改**。

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "骨架：設定驗證與健康檢查"
```

---

### Task 2: DB 連線池、IAM token 工廠、migration

**Files:**
- Create: `app/db.py`, `migrations/001_init.sql`
- Modify: `app/main.py`（lifespan 跑 migration）、`app/routers/health.py`（加 `db` 區塊）
- Test: `tests/test_db_token.py`

**Interfaces:**
- Produces: `app.db.get_conn()`（context manager，從池借還）、`app.db.iam_token() -> str`（依 `expires_in` 快取）、`app.db.run_migrations()`、`app.db.db_status() -> dict`

**關鍵：`iam_token()` 在每次「建立新連線」時呼叫。快取以 `expires_in` 為準，剩餘 < 60 秒重取。絕不在啟動時取一次就永久使用。**

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_db_token.py
import app.db as db

def test_token_cached_within_validity(monkeypatch):
    calls = []
    def fake_fetch():
        calls.append(1)
        return "tok-%d" % len(calls), 3600
    monkeypatch.setattr(db, "_fetch_token", fake_fetch)
    db._token_cache = None
    assert db.iam_token() == "tok-1"
    assert db.iam_token() == "tok-1"          # 第二次走快取
    assert len(calls) == 1

def test_token_refetched_when_near_expiry(monkeypatch):
    calls = []
    def fake_fetch():
        calls.append(1)
        return "tok-%d" % len(calls), 30       # 30 秒 < 60 秒門檻
    monkeypatch.setattr(db, "_fetch_token", fake_fetch)
    db._token_cache = None
    assert db.iam_token() == "tok-1"
    assert db.iam_token() == "tok-2"          # 快到期，重取
    assert len(calls) == 2
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_db_token.py -v` → FAIL

- [ ] **Step 3: 實作 db.py**

`_fetch_token()` 打 metadata server 回 `(access_token, expires_in)`。
`iam_token()` 用模組級 `_token_cache = (token, expires_at)` 加 `threading.Lock`。
連線池用 `queue.LifoQueue(maxsize=settings.db_pool_max)`，工廠：

```python
def _new_conn():
    return pg8000.dbapi.connect(
        user=settings.db_user,
        password=iam_token(),                       # 每條新連線都取（快取內即重用）
        database=settings.db_name,
        unix_sock=f"/cloudsql/{settings.db_instance}/.s.PGSQL.5432",
    )
```

`run_migrations()` 先 `pg_advisory_lock(<固定常數>)`，比對 `schema_migrations`，依檔名順序執行未套用的 `migrations/*.sql`，最後解鎖。

- [ ] **Step 4: 寫 migrations/001_init.sql**

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id    text NOT NULL,
  key_hash     text NOT NULL UNIQUE,
  scopes       text[] NOT NULL,
  active       boolean NOT NULL DEFAULT true,
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS api_keys_hash_active ON api_keys (key_hash) WHERE active;

CREATE TABLE IF NOT EXISTS plans (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id         text NOT NULL,
  paypal_product_id text NOT NULL,
  paypal_plan_id    text NOT NULL UNIQUE,
  name              text NOT NULL,
  amount            numeric(18,4) NOT NULL,
  currency          text NOT NULL,
  interval_unit     text NOT NULL DEFAULT 'MONTH',
  interval_count    integer NOT NULL DEFAULT 1,
  status            text NOT NULL DEFAULT 'ACTIVE',
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plans_caller ON plans (caller_id, created_at DESC);

CREATE TABLE IF NOT EXISTS orders (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id       text NOT NULL,
  reference_id    text NOT NULL,
  paypal_order_id text UNIQUE,
  amount          numeric(18,4) NOT NULL,
  currency        text NOT NULL,
  status          text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  captured_at     timestamptz,
  UNIQUE (caller_id, reference_id)
);
CREATE INDEX IF NOT EXISTS orders_caller ON orders (caller_id, created_at DESC);

CREATE TABLE IF NOT EXISTS subscriptions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id              text NOT NULL,
  plan_id                uuid NOT NULL REFERENCES plans(id),
  reference_id           text NOT NULL,
  paypal_subscription_id text UNIQUE,
  status                 text NOT NULL,
  current_period_end     timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (caller_id, reference_id)
);
CREATE INDEX IF NOT EXISTS subscriptions_caller ON subscriptions (caller_id, created_at DESC);

CREATE TABLE IF NOT EXISTS events (
  id              bigserial PRIMARY KEY,
  paypal_event_id text NOT NULL UNIQUE,
  event_type      text NOT NULL,
  caller_id       text,                    -- NULL = 對應不到 caller，對每個人都不可見
  subject_kind    text,
  subject_id      text,
  payload         jsonb NOT NULL,
  received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_caller_cursor ON events (caller_id, id);
```

- [ ] **Step 5: 測試通過並 commit**

Run: `python -m pytest tests/ -v` → PASS
```bash
git add -A && git commit -m "DB 連線池、IAM token 工廠、初始 schema"
```

---

### Task 3: API key 認證與 scope

**Files:**
- Create: `app/auth.py`, `app/errors.py`, `app/store/__init__.py`, `app/store/api_keys.py`, `scripts/add-caller.sh`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `app.auth.Caller`（dataclass：`caller_id: str`, `scopes: frozenset`）
- Produces: `app.auth.require(*scopes)` → FastAPI dependency，回 `Caller`
- Produces: `app.store.api_keys.lookup(key_hash) -> Optional[dict]`、`touch(key_id)`
- Produces: `app.auth.hash_key(plaintext) -> str`（`hashlib.sha256(plaintext.encode()).hexdigest()`）

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_auth.py
def test_missing_key_is_401(client):
    assert client.get("/v1/orders").status_code == 401

def test_bad_key_is_401(client):
    assert client.get("/v1/orders", headers={"X-API-Key": "nope"}).status_code == 401

def test_inactive_key_is_401(client, inactive_key):
    r = client.get("/v1/orders", headers={"X-API-Key": inactive_key})
    assert r.status_code == 401

def test_insufficient_scope_is_403(client, key_without_orders_read):
    r = client.get("/v1/orders", headers={"X-API-Key": key_without_orders_read})
    assert r.status_code == 403
    assert "orders:read" in r.json()["detail"]

def test_valid_key_passes(client, caller_key):
    assert client.get("/v1/orders", headers={"X-API-Key": caller_key}).status_code == 200

def test_hash_is_sha256():
    from app.auth import hash_key
    assert hash_key("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
```

- [ ] **Step 2: 執行確認失敗** → `python -m pytest tests/test_auth.py -v`

- [ ] **Step 3: 實作 auth.py 與 store/api_keys.py**

`require(*needed)` 回傳一個依賴函式：取 `X-API-Key` 標頭 → `hash_key` → `lookup` →
找不到或 `active=false` 一律 `HTTPException(401, "invalid api key")`（**不區分原因**）→
scope 不足 `HTTPException(403, f"需要 scope: {缺的}")` → `touch(key_id)` 更新 `last_used_at` → 回 `Caller`。

- [ ] **Step 4: 寫 scripts/add-caller.sh**

用法 `./scripts/add-caller.sh <env> <caller_id> "<scopes 逗號分隔>" "<note>"`。
以 `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'` 產 key，
`shasum -a 256` 算 hash，透過 `cloud-sql-proxy` + `psql` 以**內建 `postgres` 帳號**
INSERT，最後把**明文 key 印出來一次**並提示不會再顯示。

- [ ] **Step 5: 測試通過並 commit**

```bash
git add -A && git commit -m "API key 認證與 scope 檢查"
```

---

### Task 4: PayPal client（OAuth token 快取、錯誤轉譯）

**Files:**
- Create: `app/paypal/__init__.py`, `app/paypal/client.py`
- Modify: `app/routers/health.py`（加 `paypal.token`）
- Test: `tests/test_paypal_client.py`

**Interfaces:**
- Produces: `app.paypal.client.access_token() -> str`（依 `expires_in` 快取，剩 60 秒重取）
- Produces: `app.paypal.client.call(method, path, json=None, headers=None) -> dict`
- Produces: `app.paypal.client.PayPalError(Exception)`，屬性 `status: int`, `name: str`, `debug_id: str`, `details: list`

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_paypal_client.py
import pytest, app.paypal.client as pc

def test_token_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "_fetch_token",
                        lambda: (calls.append(1), ("t%d" % len(calls), 32400))[1])
    pc._token_cache = None
    assert pc.access_token() == "t1"
    assert pc.access_token() == "t1"
    assert len(calls) == 1

def test_token_refetched_near_expiry(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "_fetch_token",
                        lambda: (calls.append(1), ("t%d" % len(calls), 30))[1])
    pc._token_cache = None
    assert pc.access_token() == "t1"
    assert pc.access_token() == "t2"

def test_paypal_error_carries_debug_id(httpx_mock):
    httpx_mock.add_response(status_code=422, json={
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "d123",
        "details": [{"issue": "DUPLICATE_INVOICE_ID"}]})
    with pytest.raises(pc.PayPalError) as e:
        pc.call("POST", "/v2/checkout/orders", json={})
    assert e.value.debug_id == "d123"
    assert e.value.name == "UNPROCESSABLE_ENTITY"
```

- [ ] **Step 2: 執行確認失敗**

- [ ] **Step 3: 實作 client.py**

`_fetch_token()` 對 `{base}/v1/oauth2/token` 以 HTTP Basic（client_id / client_secret）
POST `grant_type=client_credentials`，回 `(access_token, expires_in)`。
`call()` 帶 `Authorization: Bearer`、`settings.paypal_timeout_seconds`，
非 2xx 丟 `PayPalError`。**記錄日誌時只記 `debug_id` 與 `name`，不記 token 與 body。**

- [ ] **Step 4: 測試通過並 commit**

```bash
git add -A && git commit -m "PayPal client：token 快取與錯誤轉譯"
```

---

### Task 5: Orders（建單 / capture / refund / 查詢 / 列表）

**Files:**
- Create: `app/money.py`, `app/models.py`, `app/paypal/orders.py`, `app/store/orders.py`, `app/routers/orders.py`
- Modify: `app/main.py`
- Test: `tests/test_money.py`, `tests/test_orders.py`

**Interfaces:**
- Produces: `app.money.validate_amount(amount: str, currency: str) -> Decimal`（幣別不支援丟 `UnsupportedCurrency`；小數位數超過該幣別上限丟 `InvalidAmount`）
- Produces: `app.money.DECIMALS: dict[str,int]` = `{"USD": 2, "TWD": 0, "JPY": 0, "HUF": 0}`
- Produces: `app.store.orders.create/get/list_/mark_captured/update_status`（全部第一參數 `caller_id`）
- Produces: `app.paypal.orders.create_order(...)`, `capture_order(...)`, `refund_capture(...)`

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_money.py
import pytest
from decimal import Decimal
from app.money import validate_amount, UnsupportedCurrency, InvalidAmount

def test_usd_two_decimals_ok():
    assert validate_amount("10.00", "USD") == Decimal("10.00")

def test_usd_three_decimals_rejected():
    with pytest.raises(InvalidAmount):
        validate_amount("10.000", "USD")

def test_twd_decimals_rejected():
    # 帳號目前不支援 TWD，但小數規則本身要正確
    with pytest.raises(UnsupportedCurrency):
        validate_amount("300.00", "TWD")

def test_unsupported_currency():
    with pytest.raises(UnsupportedCurrency):
        validate_amount("10.00", "EUR")

def test_zero_and_negative_rejected():
    for bad in ("0.00", "-1.00"):
        with pytest.raises(InvalidAmount):
            validate_amount(bad, "USD")
```

```python
# tests/test_orders.py
def test_create_order_returns_approve_link(client, caller_key, paypal_mock):
    r = client.post("/v1/orders", headers={"X-API-Key": caller_key}, json={
        "reference_id": "ref-1", "amount": "10.00", "currency": "USD"})
    assert r.status_code == 201
    assert r.json()["approve_url"].startswith("https://")

def test_duplicate_reference_id_is_idempotent(client, caller_key, paypal_mock):
    body = {"reference_id": "ref-dup", "amount": "10.00", "currency": "USD"}
    first = client.post("/v1/orders", headers={"X-API-Key": caller_key}, json=body)
    second = client.post("/v1/orders", headers={"X-API-Key": caller_key}, json=body)
    assert second.status_code == 200            # 冪等，不是錯誤
    assert second.json()["id"] == first.json()["id"]

def test_non_usd_rejected_before_calling_paypal(client, caller_key, paypal_mock):
    r = client.post("/v1/orders", headers={"X-API-Key": caller_key}, json={
        "reference_id": "ref-2", "amount": "300", "currency": "TWD"})
    assert r.status_code == 400
    assert "USD" in r.text
    assert paypal_mock.call_count == 0          # 沒有浪費外部呼叫

def test_other_callers_order_is_404(client, caller_key, other_callers_order_id):
    r = client.get(f"/v1/orders/{other_callers_order_id}",
                   headers={"X-API-Key": caller_key})
    assert r.status_code == 404                 # 不是 403

def test_paypal_receives_caller_attribution(client, caller_key, paypal_mock):
    client.post("/v1/orders", headers={"X-API-Key": caller_key}, json={
        "reference_id": "ref-3", "amount": "10.00", "currency": "USD"})
    pu = paypal_mock.last_request_json["purchase_units"][0]
    assert pu["custom_id"] == "test-caller"
    assert pu["invoice_id"] == "test-caller:ref-3"
```

- [ ] **Step 2: 執行確認失敗**

- [ ] **Step 3: 實作**

`POST /v1/orders` 流程：驗 scope → `validate_amount` → 查 `(caller_id, reference_id)`
已存在就回 200 + 原本那筆 → 否則本地先 INSERT（狀態 `PENDING`）→ 呼叫 PayPal
`POST /v2/checkout/orders`（帶 `custom_id=caller_id`、`reference_id=本地 id`、
`invoice_id=<caller_id>:<reference_id>`）→ 回填 `paypal_order_id` 與狀態 → 201 + `approve_url`。

PayPal 逾時或 5xx → 502，訂單留在 `PENDING`，caller 可用同一個 `reference_id` 重試。

- [ ] **Step 4: 測試通過並 commit**

```bash
git add -A && git commit -m "Orders：建單、capture、refund、查詢"
```

---

### Task 6: Plans（方案管理）

**Files:**
- Create: `app/paypal/plans.py`, `app/store/plans.py`, `app/routers/plans.py`
- Modify: `app/main.py`, `app/models.py`
- Test: `tests/test_plans.py`

**Interfaces:**
- Produces: `app.paypal.plans.create_product(name)`, `create_plan(product_id, name, amount, currency, interval_count)`, `deactivate_plan(plan_id)`
- Produces: `app.store.plans.create/get/list_/set_status`（第一參數 `caller_id`）

`POST /v1/plans` 一次做兩件事：先 `POST /v1/catalogs/products` 建 product，
再 `POST /v1/billing/plans` 建 monthly plan（`frequency: {interval_unit: "MONTH", interval_count: 1}`、
`tenure_type: "REGULAR"`、`total_cycles: 0` 代表無限期），兩個 id 都存進本地 `plans`。

- [ ] **Step 1: 寫失敗的測試**

```python
def test_create_plan_creates_product_then_plan(client, caller_key, paypal_mock):
    r = client.post("/v1/plans", headers={"X-API-Key": caller_key}, json={
        "name": "Basic 月費", "amount": "9.99", "currency": "USD"})
    assert r.status_code == 201
    assert paypal_mock.paths == ["/v1/catalogs/products", "/v1/billing/plans"]

def test_plan_currency_must_be_supported(client, caller_key, paypal_mock):
    r = client.post("/v1/plans", headers={"X-API-Key": caller_key}, json={
        "name": "x", "amount": "300", "currency": "TWD"})
    assert r.status_code == 400
    assert paypal_mock.call_count == 0

def test_plans_are_caller_scoped(client, caller_key, other_callers_plan):
    ids = [p["id"] for p in client.get(
        "/v1/plans", headers={"X-API-Key": caller_key}).json()["items"]]
    assert other_callers_plan not in ids
```

- [ ] **Step 2-4:** 確認失敗 → 實作 → 測試通過

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Plans：方案管理 API"
```

---

### Task 7: Subscriptions（月訂閱）

**Files:**
- Create: `app/paypal/subscriptions.py`, `app/store/subscriptions.py`, `app/routers/subscriptions.py`
- Modify: `app/main.py`, `app/models.py`
- Test: `tests/test_subscriptions.py`

**Interfaces:**
- Produces: `app.paypal.subscriptions.create_subscription(plan_id, custom_id, ...)`, `cancel_subscription(sub_id, reason)`
- Produces: `app.store.subscriptions.create/get/list_/update_status/set_period_end`

- [ ] **Step 1: 寫失敗的測試**

```python
def test_create_subscription_returns_approve_link(client, caller_key, paypal_mock, plan_id):
    r = client.post("/v1/subscriptions", headers={"X-API-Key": caller_key}, json={
        "reference_id": "sub-1", "plan_id": plan_id})
    assert r.status_code == 201
    assert r.json()["approve_url"].startswith("https://")
    assert r.json()["status"] == "APPROVAL_PENDING"

def test_subscription_reference_id_is_idempotent(client, caller_key, paypal_mock, plan_id):
    body = {"reference_id": "sub-dup", "plan_id": plan_id}
    a = client.post("/v1/subscriptions", headers={"X-API-Key": caller_key}, json=body)
    b = client.post("/v1/subscriptions", headers={"X-API-Key": caller_key}, json=body)
    assert b.status_code == 200 and b.json()["id"] == a.json()["id"]

def test_cannot_subscribe_to_another_callers_plan(client, caller_key, other_callers_plan):
    r = client.post("/v1/subscriptions", headers={"X-API-Key": caller_key}, json={
        "reference_id": "sub-x", "plan_id": other_callers_plan})
    assert r.status_code == 404

def test_paypal_receives_custom_id(client, caller_key, paypal_mock, plan_id):
    client.post("/v1/subscriptions", headers={"X-API-Key": caller_key}, json={
        "reference_id": "sub-2", "plan_id": plan_id})
    assert paypal_mock.last_request_json["custom_id"] == "test-caller"
```

- [ ] **Step 2-4:** 確認失敗 → 實作 → 測試通過

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Subscriptions：月訂閱建立與取消"
```

---

### Task 8: Webhook 接收與事件流

**Files:**
- Create: `app/paypal/webhooks.py`, `app/store/events.py`, `app/routers/webhooks.py`, `app/routers/events.py`
- Modify: `app/main.py`, `app/routers/health.py`（`webhook: configured/unconfigured`）
- Test: `tests/test_webhooks.py`, `tests/test_events.py`

**Interfaces:**
- Produces: `app.paypal.webhooks.verify(raw_body: bytes, headers: dict) -> bool`
- Produces: `app.store.events.record(paypal_event_id, event_type, caller_id, subject_kind, subject_id, payload) -> Optional[int]`（重複回 `None`）
- Produces: `app.store.events.list_after(caller_id, after: int, limit: int) -> list[dict]`

- [ ] **Step 1: 寫失敗的測試**

```python
# tests/test_webhooks.py
def test_unconfigured_webhook_id_returns_503(client_without_webhook_id):
    r = client_without_webhook_id.post("/v1/webhooks", json={"id": "e1"})
    assert r.status_code == 503

def test_bad_signature_is_401_and_not_stored(client, verify_fails, db):
    r = client.post("/v1/webhooks", json={"id": "e-bad", "event_type": "X"})
    assert r.status_code == 401
    assert db.count_events() == 0            # 驗簽失敗不落地

def test_verification_uses_raw_bytes(client, verify_spy):
    body = b'{"id":"e2","event_type":"PAYMENT.SALE.COMPLETED",  "spaced":1}'
    client.post("/v1/webhooks", content=body,
                headers={"Content-Type": "application/json"})
    assert verify_spy.raw_body == body       # 逐位元組相同，不是重新序列化的

def test_duplicate_event_is_noop(client, verify_ok, db):
    ev = {"id": "e3", "event_type": "PAYMENT.SALE.COMPLETED", "resource": {}}
    assert client.post("/v1/webhooks", json=ev).status_code == 200
    assert client.post("/v1/webhooks", json=ev).status_code == 200
    assert db.count_events() == 1

def test_unmappable_event_stored_with_null_caller(client, verify_ok, db):
    client.post("/v1/webhooks", json={
        "id": "e4", "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"billing_agreement_id": "I-UNKNOWN"}})
    assert db.last_event()["caller_id"] is None

def test_subscription_payment_updates_status(client, verify_ok, db, active_sub):
    client.post("/v1/webhooks", json={
        "id": "e5", "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"billing_agreement_id": active_sub.paypal_id}})
    assert db.get_sub(active_sub.id)["status"] == "ACTIVE"
```

```python
# tests/test_events.py
def test_cursor_returns_only_newer(client, caller_key, seeded_events):
    first = client.get("/v1/events?after=0&limit=2",
                       headers={"X-API-Key": caller_key}).json()["items"]
    assert len(first) == 2
    nxt = client.get(f"/v1/events?after={first[-1]['id']}",
                     headers={"X-API-Key": caller_key}).json()["items"]
    assert all(e["id"] > first[-1]["id"] for e in nxt)

def test_null_caller_events_invisible(client, caller_key, orphan_event):
    ids = [e["id"] for e in client.get(
        "/v1/events?after=0", headers={"X-API-Key": caller_key}).json()["items"]]
    assert orphan_event not in ids

def test_other_callers_events_invisible(client, caller_key, other_callers_event):
    ids = [e["id"] for e in client.get(
        "/v1/events?after=0", headers={"X-API-Key": caller_key}).json()["items"]]
    assert other_callers_event not in ids

def test_limit_over_max_is_400(client, caller_key):
    assert client.get("/v1/events?after=0&limit=501",
                      headers={"X-API-Key": caller_key}).status_code == 400
```

- [ ] **Step 2: 執行確認失敗**

- [ ] **Step 3: 實作**

`POST /v1/webhooks` 流程：`settings.paypal_webhook_id is None` → 503 →
`raw = await request.body()` → `verify(raw, headers)` 失敗 → 401 且不落地 →
解析事件 → 依 `resource` 推導 `caller_id`（訂單走 `paypal_order_id`、
訂閱走 `billing_agreement_id`／`resource.id`；推不出來就 `None`）→
`record()`（`paypal_event_id` 重複回 `None`，視為 no-op）→ 更新對應的訂單／訂閱狀態 → 200。

`GET /v1/events`：`limit` 預設 100、上限 500，超過回 400。

- [ ] **Step 4: 測試通過並 commit**

```bash
git add -A && git commit -m "Webhook 接收、簽章驗證與事件流"
```

---

### Task 9: 開通與部署到 dev

**這一段是人／工具跑的一次性動作，不是程式碼。順序不可顛倒（第 6 步有雞生蛋）。**

- [ ] **Step 1: 建 repo**

```bash
gh repo create adamsourceinfo-org/payment-paypal --public --source=. --remote=origin --push
```

- [ ] **Step 2: PayPal sandbox app**

在 Developer 後台 sandbox 建 app `payment-paypal`，取得 client id 與 secret。

- [ ] **Step 3: Cloud SQL（dev）**

依 `ci/README.md` 的「開 instance」：建 `payment-paypal-pg`、database `payment_paypal`、
IAM 使用者 `run-runtime@adamsourceinfo-dev.iam`、授 `cloudsql.client` + `cloudsql.instanceUser`、
**連進 `payment_paypal` 下** `GRANT`。建 instance 要 `--edition=ENTERPRISE`。

- [ ] **Step 4: Secret Manager（dev）**

```bash
gcloud secrets create payment-paypal-client-secret-dev \
  --replication-policy=automatic --project=adamsourceinfo-dev
gcloud secrets add-iam-policy-binding payment-paypal-client-secret-dev \
  --project=adamsourceinfo-dev --role=roles/secretmanager.secretAccessor \
  --member="serviceAccount:run-runtime@adamsourceinfo-dev.iam.gserviceaccount.com"
```

- [ ] **Step 5: 第一次部署**

推 main 觸發 CI。此時 `PAYPAL_WEBHOOK_ID` 是空的，服務會以降級狀態啟動。
**這一次的驗證重點是 `--update-secrets`** —— ci 唯一沒端對端驗過的路徑。
確認 `/healthz` 的 `paypal.token == "ok"`（代表 secret 有掛上且換得到 token）。

- [ ] **Step 6: 註冊 webhook**

取得 Cloud Run URL → PayPal 後台建 webhook 指向 `<url>/v1/webhooks`，
訂閱：`CHECKOUT.ORDER.APPROVED`、`PAYMENT.CAPTURE.COMPLETED`、`PAYMENT.CAPTURE.DENIED`、
`PAYMENT.CAPTURE.REFUNDED`、`BILLING.SUBSCRIPTION.ACTIVATED`、`BILLING.SUBSCRIPTION.CANCELLED`、
`BILLING.SUBSCRIPTION.SUSPENDED`、`BILLING.SUBSCRIPTION.EXPIRED`、
`BILLING.SUBSCRIPTION.PAYMENT.FAILED`、`PAYMENT.SALE.COMPLETED`
→ 取得 webhook id 填進 `.cicd/env.dev` → 推第二次。

- [ ] **Step 7: max-instances**

```bash
gcloud run services update payment-paypal --max-instances=10 \
  --project=adamsourceinfo-dev --region=asia-east1
```

db-f1-micro 的 `max_connections` 約 25，Cloud Run 預設可擴到 100 實例 × pool 3 會打爆。

- [ ] **Step 8: 建第一把 API key**

```bash
./scripts/add-caller.sh dev smoke-test \
  "orders:read,orders:write,plans:read,plans:write,subscriptions:read,subscriptions:write,events:read" \
  "sandbox 實測用"
```

---

### Task 10: sandbox 實測（USD）

**對已部署的 dev 服務打真實 PayPal sandbox。每一項都要看到實際回應，不能只看 HTTP 200。**

- [ ] **Step 1: 健康檢查證明得了東西**

`/healthz` 的 `db.server_user` 與 `db.database` 必須由 DB 自己回答
（`run-runtime@adamsourceinfo-dev.iam` / `payment_paypal`），不是環境變數回音。
`paypal.token == "ok"`、`paypal.webhook == "configured"`。

- [ ] **Step 2: 認證與隔離**

無 key → 401；亂 key → 401；scope 不足 → 403；查別的 caller 的資源 → 404。

- [ ] **Step 3: Orders**

建單（USD 10.00）→ 拿到 `approve_url` → 非 USD 被擋在 400 且沒打 PayPal →
同一個 `reference_id` 重送回 200 + 同一筆 → 列表看得到 → 用 sandbox 買家帳號走完
`approve_url` → capture → 查詢狀態為 `COMPLETED` → refund。

- [ ] **Step 4: `invoice_id` 重複阻擋（spec 標記為未驗證項）**

用**不同的** `reference_id` 但**相同的** `invoice_id` 直接打 PayPal，看是否回
`DUPLICATE_INVOICE_ID`。**同時去 PayPal 帳號的付款接收偏好設定確認
「Block accidental payments」是開是關** —— 這才是決定行為的地方。
把結果寫回 spec 的「已知風險與未驗證項目」。

- [ ] **Step 5: Plans 與 Subscriptions**

建方案（USD 9.99/月）→ 確認 PayPal 上 product 與 plan 都建起來 →
建訂閱 → 拿到 `approve_url` → 用 sandbox 買家帳號同意 →
確認收到 `BILLING.SUBSCRIPTION.ACTIVATED` webhook → 訂閱狀態變 `ACTIVE`。

- [ ] **Step 6: 事件流**

`GET /v1/events?after=0` 看得到上述所有事件、游標遞增正確、
別的 caller 與 `caller_id IS NULL` 的事件都看不到。

- [ ] **Step 7: 把實測結果寫進 spec 並 commit**

未驗證項目表更新：`--update-secrets` 與 `invoice_id` 兩項填上實際結果。

---

### Task 11: 打 tag 上 prod

**前提：Task 10 全部通過。**

- [ ] **Step 1: 確認 prod 的前置**

prod 的 Cloud SQL、Secret（live client secret）、live PayPal app 與 webhook
都要比照 Task 9 開通。**prod 用 live 憑證，會產生真實金流。**

- [ ] **Step 2: 打 tag**

```bash
git tag v0.1.0 && git push origin v0.1.0
```

tag 走 promote 路徑：**不重新 build**，把 main 建好的同一個 digest 搬到 prod。

- [ ] **Step 3: 確認 prod**

`/healthz` 的 `db.instance` 與 `db.server_user` 必須指向 **prod** 專案，
`paypal.env == "live"`。兩邊跑同一個 image，差異只可能來自 CI 推導與 `.cicd/` 設定。
