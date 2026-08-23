# payment-paypal 設計

2026-08-23

## 這是什麼

一支底層後端服務，把 PayPal 包起來供 `adamsourceinfo` 底下其他服務呼叫。
呼叫方（caller）用 API key 認證，服務負責建立與管理**一次性訂單**與**月訂閱**。

不是給終端使用者用的，沒有網頁介面。對外只有 JSON API 與 OpenAPI 文件。

部署到 `adamsourceinfo-dev` / `adamsourceinfo-prod`，走 `adamsourceinfo-org/ci` 的共用流程。

### 跟舊服務的關係

`adamhsu-apps` 專案裡有一個同名的 `payment-paypal`。**本專案是全新重寫，不碰舊資料、不相容舊 API。**
舊服務繼續跑，是否下線之後再決定。同名是刻意的 —— 將來真要下線舊的時比較順。

---

## 決策紀錄

每一條都是已定案的選擇，附上理由，避免日後重新爭論。

| # | 決策 | 理由 |
|---|---|---|
| 1 | 訂單存在自己的 Cloud SQL，不做純轉發代理 | 金流服務沒有自己的帳就對不起帳。冪等、caller 歸屬、爭議舉證都需要本地紀錄 |
| 2 | 方案（plan）由本服務提供管理 API，不是寫死在設定檔 | caller 不必碰 PayPal 後台；方案與訂閱在同一套 API 裡 |
| 3 | caller 靠**拉取 + 事件流**得知狀態變化，本服務不主動推送 | 推送要一整套可靠送達子系統（重試、退避、死信）。caller 會持續增加，推送的營運負擔全落在本服務身上；拉取的成本落在 caller 自己身上 |
| 4 | API key 每 caller 一把，存 DB，帶權限，只存 hash | 可單獨停用、可稽核、新增 caller 不必重新部署 |
| 5 | 機密走 Secret Manager，部署時掛成環境變數 | `ci` 契約原生支援。執行時呼叫 Secret Manager API 的額外安全性很有限（見「安全邊界」） |
| 6 | PayPal API base URL 由 `PAYPAL_ENV` 推導，不做成設定 | 可設定就有設錯的餘地，而「prod 指到 sandbox」的代價是以為在收錢但沒有 |
| 7 | 一環境一組 PayPal Client ID，不是每 caller 一組 | Client ID 認證的是「錢進哪個商家帳號」，跟 caller 無關。PayPal 的模型裡不存在 per-caller 憑證 |
| 8 | 不做 admin API、不存在 bootstrap admin key | API key 用 `scripts/add-caller.sh` 手動建立。萬能鑰匙的長期風險大於它省下的麻煩 |
| 9 | 服務名 `payment-paypal`，webhook 路徑 `/v1/webhooks` | — |

### 明確不做的

- **PayPal Multiparty（多商家收款）**。已確認所有 caller 都是自己的服務，錢進同一個帳號。
  將來若要支援外部商家收自己的錢，那是 Partner Referrals + `PayPal-Auth-Assertion` 的另一個等級，
  屆時另開專案期程。資料模型不為此預留欄位（YAGNI）。
- **主動推送事件給 caller**。事件表就是將來要做推送時的來源，不會白做。
- **PayPal 以外的金流**。服務名綁 PayPal 是刻意的。

---

## 對外介面

所有端點前綴 `/v1`。除 `/v1/webhooks` 與 `/health` 外都需要 `X-API-Key` 標頭。

```
POST   /v1/orders                    建立一次性訂單，回 PayPal 付款連結     orders:write
POST   /v1/orders/{id}/capture       消費者授權後扣款                       orders:write
POST   /v1/orders/{id}/refund        退款（全額或部分）                     orders:write
GET    /v1/orders                    列自己的訂單（分頁、依狀態篩）          orders:read
GET    /v1/orders/{id}                                                      orders:read

POST   /v1/plans                     建方案（同步建 PayPal product + plan）  plans:write
GET    /v1/plans                                                            plans:read
POST   /v1/plans/{id}/deactivate     停售                                   plans:write

POST   /v1/subscriptions             建訂閱，回 PayPal 同意連結             subscriptions:write
POST   /v1/subscriptions/{id}/cancel                                        subscriptions:write
GET    /v1/subscriptions                                                    subscriptions:read
GET    /v1/subscriptions/{id}                                               subscriptions:read

GET    /v1/events?after=<cursor>&limit=100   增量拉自己的事件               events:read

POST   /v1/webhooks                  PayPal 專用。不驗 API key，驗 PayPal 簽章
GET    /health                       健康檢查。不驗（不能用 /healthz，見下）
```

FastAPI 自動產生 OpenAPI 文件，其他服務照著接。

### 事件流的契約

`GET /v1/events?after=<cursor>` 回傳 `id > cursor` 且屬於自己的事件，依 `id` 遞增。
caller 記住最後一筆的 `id` 當下次的 `after`。`limit` 預設 100、上限 500，超過上限的請求回 400。

首次接入傳 `after=0` 可以從頭拉，這也是對帳的路徑。

---

## 認證與授權

### API key

- caller 呼叫時帶 `X-API-Key: <明文 key>`
- DB 只存 `sha256(key)`，**不存明文**。key 遺失只能重發，不能找回
- 驗證通過後把 `caller_id` 與 `scopes` 放進請求上下文
- 每次成功驗證更新 `last_used_at`（用於盤點死掉的 key）
- `active = false` 立即失效，不需重新部署

權限（scope）：

```
orders:read  orders:write
plans:read   plans:write
subscriptions:read  subscriptions:write
events:read
```

### caller 隔離

**每一張業務表都有 `caller_id`，`store/` 層的每一個查詢都強制帶 `WHERE caller_id = :me`。**
隔離是查詢層的預設值，不是靠呼叫端記得傳參數。

### 服務對外是公開的

Cloud Run 設 `allow_unauthenticated: true` —— 因為 PayPal 必須打得到 webhook。
這表示**應用層的 API key 是唯一一道門**。所以 key 存 hash、可停用、有稽核欄位。

---

## 資料模型

Postgres 17。所有時間欄位 `timestamptz`。

### `api_keys`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | uuid pk | |
| `caller_id` | text not null | caller 識別碼，例如 `line-bot`、`web-shop` |
| `key_hash` | text not null unique | `sha256(明文 key)` |
| `scopes` | text[] not null | |
| `active` | bool not null default true | |
| `note` | text | 這把 key 給誰、什麼時候發的 |
| `created_at` / `last_used_at` | timestamptz | |

### `plans`

| 欄位 | 說明 |
|---|---|
| `id` uuid pk | 本地識別碼，對外用這個 |
| `caller_id` | |
| `paypal_product_id` / `paypal_plan_id` | PayPal 那邊的對應物 |
| `name` / `amount` / `currency` / `interval_unit`（固定 `MONTH`）/ `interval_count` | |
| `status` | `ACTIVE` / `INACTIVE` |
| `created_at` | |

### `orders`

| 欄位 | 說明 |
|---|---|
| `id` uuid pk | |
| `caller_id` | |
| `reference_id` text not null | **caller 提供的冪等鍵** |
| `paypal_order_id` text unique | |
| `amount` / `currency` / `status` | |
| `created_at` / `updated_at` / `captured_at` | |

`UNIQUE (caller_id, reference_id)` —— 網路重試不會變成兩筆收款。重複請求回原本那筆，回 200 不報錯。

### `subscriptions`

| 欄位 | 說明 |
|---|---|
| `id` uuid pk | |
| `caller_id` / `plan_id` | |
| `reference_id` text not null | 同樣的冪等鍵 |
| `paypal_subscription_id` text unique | |
| `status` | `APPROVAL_PENDING` / `ACTIVE` / `SUSPENDED` / `CANCELLED` / `EXPIRED` |
| `current_period_end` | 由 webhook 更新 |
| `created_at` / `updated_at` | |

`UNIQUE (caller_id, reference_id)`。

### `events`

| 欄位 | 說明 |
|---|---|
| `id` bigserial pk | **就是對外的游標** |
| `paypal_event_id` text not null unique | PayPal 重送時的冪等保護 |
| `event_type` text | 例如 `PAYMENT.SALE.COMPLETED` |
| `caller_id` text **nullable** | 見下 |
| `subject_kind` / `subject_id` | 關聯到哪筆訂單或訂閱 |
| `payload` jsonb | webhook 原文，爭議時的證據 |
| `received_at` | |

**`caller_id` 可以是 NULL。** 有些事件對應不到任何 caller —— 例如關聯到的資源不是本服務建的
（同一個 PayPal 商家帳號底下還有 `line-translate-bot` 等其他 app）。這類事件**照樣落地**
（不丟棄，保留原文），但 `caller_id IS NULL` 讓它對每一個 caller 都不可見。
`GET /v1/events` 一律 `WHERE caller_id = :me`，NULL 不會匹配任何人。

### `schema_migrations`

migration 版本紀錄。啟動時執行 `migrations/*.sql`，用 Postgres advisory lock
（`pg_advisory_lock`）避免多個 Cloud Run 實例同時跑。

---

## PayPal 整合

### 一環境一組憑證

| | dev | prod |
|---|---|---|
| PayPal app | `payment-paypal`（sandbox） | `payment-paypal-live`（live） |
| API base | `https://api-m.sandbox.paypal.com` | `https://api-m.paypal.com` |
| 商家帳號 | sandbox 測試帳號 | `adamsourceinfo@gmail.com` |

base URL 由 `PAYPAL_ENV` 推導，程式裡沒有任何寫死的 PayPal 網址。

### Access token

用 client id + secret 打 `POST /v1/oauth2/token`（`grant_type=client_credentials`）換取，
有效期約 9 小時。**只在記憶體快取，到期前 60 秒重換。不寫進 Secret Manager、不寫進 DB、不寫進日誌。**

### 建單時要寫進 PayPal 的 caller 資訊

所有 caller 的錢都進同一個 PayPal 帳號，PayPal 的報表本身分不出哪筆是誰的。
`orders.caller_id` 是唯一的歸屬紀錄 —— 所以要在 PayPal 那邊也留一份，讓對帳不必只靠本服務的 DB：

| PayPal 欄位 | 放什麼 |
|---|---|
| `purchase_units[].custom_id` | `caller_id` |
| `purchase_units[].reference_id` | 本地 `orders.id` |
| `purchase_units[].invoice_id` | `<caller_id>:<reference_id>` |

**`invoice_id` 的重複阻擋要驗證再依賴。** PayPal 只有在商家帳號啟用「Block accidental payments」
（付款接收偏好設定）時才會擋重複的 invoice id。要在 sandbox 實測，**而且要去確認那個帳號設定**，
不能只看 API 有沒有回 `DUPLICATE_INVOICE_ID`。若該設定是關的，**本服務的 DB unique 約束就是唯一的冪等保護** ——
這種情況下設計不變，只是不能宣稱有第二道防線。

### Webhook

`POST /v1/webhooks`。路徑是本服務自訂的，PayPal 不規定，只要求 HTTPS 且公網可達。

**驗簽章必須用原始 bytes。** 用 `await request.body()` 取原文交給
`POST /v1/notifications/verify-webhook-signature`。若讓 pydantic 先解析再重新序列化，位元組會變、驗證必定失敗。
這支 handler 的寫法跟其他端點不同，是刻意的。

要訂閱的事件：

| 用途 | 事件 |
|---|---|
| 訂閱生命週期 | `BILLING.SUBSCRIPTION.ACTIVATED` / `.CANCELLED` / `.SUSPENDED` / `.EXPIRED` |
| **每月扣款成功** | `PAYMENT.SALE.COMPLETED` |
| 扣款失敗 | `BILLING.SUBSCRIPTION.PAYMENT.FAILED` |
| 一次性訂單 | `CHECKOUT.ORDER.APPROVED`、`PAYMENT.CAPTURE.COMPLETED` / `.DENIED` / `.REFUNDED` |

**訂閱的每月扣款發的是 `PAYMENT.SALE.COMPLETED`，不是任何 `CHECKOUT.ORDER.*`。**
只訂 `BILLING.SUBSCRIPTION.*` 的話會知道訂閱還活著，但不會知道這個月的錢到了沒。

dev 與 prod 是**兩個獨立的 webhook 註冊**，各自產生不同的 webhook id，
所以 `PAYPAL_WEBHOOK_ID` 放 `env.dev` / `env.prod`，不能放 `env.common`。

### 金額與幣別

**本帳號只能收 USD。** 已確認的帳號限制，不是設計選擇。

實作方式：`SUPPORTED_CURRENCIES` 放 `env.common`，目前值為 `USD`。
pydantic 在進門就驗證 `currency` 在清單內，不在的話回 400 並列出支援的幣別 ——
不要送到 PayPal 才被拒（那時錯誤訊息對 caller 沒有幫助，而且浪費一次外部呼叫）。
帳號將來開通其他幣別時，改一行設定即可，不必改程式。

幣別由 caller **明確指定，沒有預設值** —— 金錢的欄位不該有預設。即使目前只支援一種也一樣：
預設值會讓「忘了傳幣別」變成靜默通過，而不是明確的錯誤。

**小數位數依幣別而定。** USD 是 2 位（`"10.00"` 合法）。
但 PayPal 對 **TWD / JPY / HUF 不接受小數**，送 `"300.00"` 會被拒。
所以小數位數要做成幣別的屬性（`{"USD": 2}`）而不是寫死 2，
否則將來開通 TWD 時會踩到這個坑。

---

## 設定與機密

### `.cicd/config.yml`

```yaml
service: payment-paypal
health_path: /health
allow_unauthenticated: true      # PayPal webhook 必須打得到

db:                              # 只宣告一次，CI 依部署目標推導連線資訊
  instance: payment-paypal-pg
  name: payment_paypal
```

### 環境變數全表

| 變數 | 誰寫 | dev | prod | 缺了會怎樣 |
|---|---|---|---|---|
| `APP_ENV` | CI 注入 | `dev` | `prod` | — |
| `APP_VERSION` | CI 注入 | image tag | 同一 digest | — |
| `PORT` | Cloud Run 注入 | 8080 | 8080 | — |
| `INSTANCE_CONNECTION_NAME` | CI 推導 | `adamsourceinfo-dev:asia-east1:payment-paypal-pg` | prod 對應 | — |
| `DB_USER` | CI 推導 | `run-runtime@adamsourceinfo-dev.iam` | prod 對應 | — |
| `DB_NAME` | CI（來自 config.yml） | `payment_paypal` | 同左 | — |
| `PAYPAL_ENV` | `env.<env>` | `sandbox` | `live` | **啟動失敗** |
| `PAYPAL_CLIENT_ID` | `env.<env>` | sandbox app 的 | live app 的 | **啟動失敗** |
| `PAYPAL_WEBHOOK_ID` | `env.<env>` | sandbox webhook 的 | live webhook 的 | 啟動成功但降級 |
| `PAYPAL_CLIENT_SECRET` | **`secrets.<env>`** | Secret Manager | Secret Manager | **啟動失敗** |
| `PAYPAL_TIMEOUT_SECONDS` | `env.common` | `10` | `10` | 用預設 10 |
| `DB_POOL_MAX` | `env.common` | `3` | `3` | 用預設 3 |
| `SUPPORTED_CURRENCIES` | `env.common` | `USD` | `USD` | 用預設 `USD` |
| `LOG_LEVEL` | `env.<env>` | `debug` | `info` | 用預設 info |

前六個是 CI 注入或推導的。`INSTANCE_CONNECTION_NAME`、`DB_USER`、`DB_INSTANCE`、`DB_NAME`
寫進 `.cicd/env.*` 會被 `verify` 擋下 —— 這讓「dev 連到 prod 的 DB」變成寫不出來的錯。

### 檔案內容

```ini
# .cicd/env.common
PAYPAL_TIMEOUT_SECONDS=10
DB_POOL_MAX=3
SUPPORTED_CURRENCIES=USD

# .cicd/env.dev
LOG_LEVEL=debug
PAYPAL_ENV=sandbox
PAYPAL_CLIENT_ID=<sandbox app 的 client id>
PAYPAL_WEBHOOK_ID=<註冊 webhook 後才有>

# .cicd/env.prod
LOG_LEVEL=info
PAYPAL_ENV=live
PAYPAL_CLIENT_ID=<live app 的 client id>
PAYPAL_WEBHOOK_ID=<註冊 webhook 後才有>

# .cicd/secrets.dev
PAYPAL_CLIENT_SECRET=payment-paypal-client-secret-dev:latest

# .cicd/secrets.prod
PAYPAL_CLIENT_SECRET=payment-paypal-client-secret-prod:latest
```

**刻意沒有 `secrets.common`** —— sandbox 與 live 的 client secret 依定義就是兩個不同的值。

### 啟動時驗證

`config.py` 啟動時讀一次、驗一次，缺少必要變數就**啟動失敗**。
Cloud Run 起不來 → CI 的 smoke 紅燈 → 當場知道。

**`PAYPAL_WEBHOOK_ID` 是唯一的例外，允許缺席** —— 有雞生蛋問題（見 runbook 第 6 步）。
缺席時服務正常啟動，`/health` 回 `"webhook": "unconfigured"`，`POST /v1/webhooks` 回 503。
不會靜靜收下無法驗簽的請求。

---

## 資料庫連線

用 Cloud SQL **IAM 資料庫認證**，Secret Manager 裡沒有任何 DB 機密。
向 metadata server 要 access token 當密碼，走 unix socket `/cloudsql/<connection_name>/.s.PGSQL.5432`。

**token 的取得必須放在連線池的「建立新連線」工廠裡，不能在啟動時取一次就快取。**
token 約一小時過期；已建立的連線不受影響（Postgres 只在連線當下驗密碼），但**每一條新連線都需要新的 token**。
在啟動時快取會造成「跑一小時後新連線開始失敗」這種很難查的問題。

---

## 模組結構

```
app/
├─ main.py              FastAPI app、router 掛載、lifespan（跑 migration）
├─ config.py            環境變數讀取與驗證，唯一碰 os.environ 的地方
├─ db.py                連線池（token 工廠）、migration runner
├─ auth.py              API key 驗證與 scope 檢查（FastAPI dependency）
├─ models.py            pydantic schema
├─ paypal/
│  ├─ client.py         OAuth token 快取、HTTP 呼叫、PayPal 錯誤轉譯
│  ├─ orders.py         Orders v2
│  ├─ plans.py          Catalog Products + Billing Plans
│  ├─ subscriptions.py  Subscriptions v1
│  └─ webhooks.py       簽章驗證
├─ routers/             orders / plans / subscriptions / events / webhooks / health
└─ store/               api_keys / orders / plans / subscriptions / events，SQL 只在這層
migrations/
└─ 001_init.sql
scripts/
└─ add-caller.sh        產生 API key、算 hash、INSERT
Dockerfile
```

分層規則：`paypal/` 只知道怎麼跟 PayPal 講話，`store/` 只知道怎麼跟 DB 講話，
`routers/` 把兩者接起來。任何一層的內部改動不影響另外兩層。

---

## 錯誤處理

| 情況 | 回應 |
|---|---|
| 沒帶 / 錯誤 / 停用的 API key | 401，不區分原因（不幫攻擊者縮小範圍） |
| key 有效但 scope 不足 | 403，訊息指出缺哪個 scope |
| 重複的 `reference_id` | 200 + 原本那筆（冪等，不是錯誤） |
| 查詢別人的資源 | 404，不是 403（不洩漏該資源存在） |
| PayPal 回 4xx | 轉譯成本服務的錯誤碼，附 PayPal 的 `debug_id` 供查詢 |
| PayPal 逾時 / 5xx | 502，訂單留在 `PENDING`，caller 可用同一個 `reference_id` 重試 |
| webhook 簽章驗證失敗 | 401，**不落地** |
| webhook 對應不到 caller | 200 + 落地為 `caller_id IS NULL` |

**任何情況都不把 client secret、access token、API key 明文寫進日誌。**
加一個 logging filter 過濾這些值。

---

## 測試

CI 不跑測試（既有決定：測試在開發時自行驗過才推）。本機 TDD。

- pytest + httpx 的 mock transport 擋掉 PayPal 呼叫，測 router 與 store 的邏輯
- DB 測試用本機 docker postgres（不連 Cloud SQL）
- webhook 簽章驗證用固定的 fixture payload
- **一項必須在 sandbox 實測，不能只靠 mock**：`invoice_id` 的重複阻擋行為與商家帳號設定

---

## 一次性 runbook（人跑的，CI 不會做）

順序不能顛倒，第 6 步有雞生蛋。

1. `gh repo create adamsourceinfo-org/payment-paypal --public --clone`
2. **PayPal Developer 後台**：sandbox 建 app `payment-paypal`、live 建 app `payment-paypal-live`，
   各拿 client id 與 secret
3. **Cloud SQL**（dev / prod 各一次，指令見 `ci` README 的「開 instance」）：
   建 instance `payment-paypal-pg`、database `payment_paypal`、IAM 使用者
   `run-runtime@<專案>.iam`、授 `cloudsql.client` + `cloudsql.instanceUser`、
   連進 `payment_paypal` 下 `GRANT`
4. **Secret Manager**（dev / prod 各一次）：
   ```bash
   gcloud secrets create payment-paypal-client-secret-dev \
     --replication-policy=automatic --project=adamsourceinfo-dev
   pbpaste | gcloud secrets versions add payment-paypal-client-secret-dev \
     --data-file=- --project=adamsourceinfo-dev
   gcloud secrets add-iam-policy-binding payment-paypal-client-secret-dev \
     --project=adamsourceinfo-dev --role=roles/secretmanager.secretAccessor \
     --member="serviceAccount:run-runtime@adamsourceinfo-dev.iam.gserviceaccount.com"
   ```
   用 `pbpaste` 是刻意的：值從剪貼簿直接進 Secret Manager，不經過終端機歷史、不經過對話紀錄。
5. 推 main，第一次部署（此時 `PAYPAL_WEBHOOK_ID` 還是空的，服務會以降級狀態啟動）
6. **拿到 Cloud Run URL** → 回 PayPal 後台建 webhook 指向 `<url>/v1/webhooks`、訂閱上面列的事件
   → 拿到 webhook id 填進 `.cicd/env.<env>` → 再推一次
7. **設定 max-instances**（dev / prod 各一次）：
   ```bash
   gcloud run services update payment-paypal --max-instances=10 \
     --project=adamsourceinfo-dev --region=asia-east1
   ```
   db-f1-micro 的 `max_connections` 約 25，而 Cloud Run 預設可以擴到 100 個實例。
   100 × `DB_POOL_MAX=3` 會輕易打爆連線數。CI 從不傳 min/max instances，所以這個手動設定會被保留。
8. **建第一把 API key**：`scripts/add-caller.sh`
9. 分支保護（required check：`deploy / 1 verify`、`deploy / 2 build`）

### `scripts/add-caller.sh`

產生一把亂數 key、算 sha256、透過 `cloud-sql-proxy` + `psql` INSERT 進 `api_keys`，
把**明文 key 印出來一次**（之後再也拿不到）。

**連線身分用內建的 `postgres` 帳號**（第 3 步 `GRANT` 時設的一次性密碼）。
runbook 只建立了 `run-runtime` 這個 IAM 資料庫使用者給服務用；
人要連的話另一個選擇是 `gcloud sql users create --type=cloud_iam_user` 建自己的 IAM 使用者，
但那多一個要管理的授權對象，這裡不做。

---

## 安全邊界

### 機密實際上住在哪

`.cicd/secrets.<env>` 裡只有名字（`payment-paypal-client-secret-dev:latest`），值在 Secret Manager。
Cloud Run 拿的是參照 —— 實測確認 `gcloud run services describe` 對這種變數只顯示 `secretKeyRef`，
不顯示值；讀到值需要該 secret 的 `secretmanager.secretAccessor`，光有 `run.viewer` 拿不到。

### 最高機率的外洩路徑：repo 是 public

`ci` 的流程用 `gh repo create --public`（Free 方案的分支保護只對 public repo 有效）。
**把 client secret 的值誤寫進 `.cicd/env.dev` 而不是 `secrets.dev`，就是憑證當場公開在 GitHub 上。**
CI 的保留字清單擋的是變數**名字**，不會知道某一行的**值**是一串 PayPal 金鑰。

這是整套設定裡外洩機率最高的一條路，而它跟 env-vs-secret 的技術選擇無關，純粹是一次手滑。

> 可選的防呆（需改 `ci` repo，跨專案，本專案範圍外）：在 `verify` 階段檢查
> `.cicd/env.*` 是否出現高熵字串或 `*_SECRET` / `*_KEY` / `*_TOKEN` 這類 key 名，命中就紅燈。

### 掛載方式不是風險所在

環境變數、檔案掛載、執行時呼叫 Secret Manager API 三者擋的是同一類攻擊者，
而那類攻擊者三種都能繞過 —— 能在容器裡執行程式碼的人（例如被下毒的相依套件）三種都讀得到。

真正的邊界是：**任何能部署的人都能把機密印出來**。在這套架構裡，部署身分只有
`adamsourceinfo-org/ci` 的 `deploy.yml@refs/heads/main` 換得到憑證。
**所以這個 secret 的實際安全邊界是 main 分支保護，不是掛載方式。**

### 輪替

`:latest` 是在**實例啟動時**解析並鎖定該實例的整個生命週期。
在低流量或設了 min-instances 的情況下，「寫新版本然後等它自己生效」的延遲沒有上界。
**輪替 = 寫新的 secret 版本 + 重新部署**，不是只寫版本。

PayPal 後台可以撤銷 / 重發 client secret，那是最後一道閘。

---

## 已知風險與未驗證項目

### 2026-08-23 dev 實測結果

| 項目 | 狀態 |
|---|---|
| `--update-secrets`（Secret Manager 掛載） | ✅ **已驗證**。`/health` 回 `paypal.token: "ok"`，代表 secret 掛上且換得到 token。ci 這條路可以標記為驗過了 |
| `run_migrations()` 自建 schema | ✅ 已驗證。刻意先把手動建的表刪掉，讓 app 自己跑；日誌 `migration 套用 ['001_init.sql']` |
| Cloud SQL IAM 認證 | ✅ `server_user` 由 DB 回答為 `run-runtime@adamsourceinfo-dev.iam`，非環境變數回音 |
| CI 不覆蓋手動設定 | ✅ 手動設的 `--max-instances=10` 在後續兩次 CI 部署後仍存在 |
| PR 拿不到 GCP 憑證 | ✅ PR run 上 6 個碰 GCP 的 job 全部 skipped |
| webhook 簽章驗證（原始 bytes） | ✅ **真實 PayPal 送達並驗過**。`CHECKOUT.ORDER.APPROVED` 進來、驗簽成功、事件落地、訂單狀態自動更新為 `APPROVED`、`/v1/events` 讀得到 |
| API 表面（34 項） | ✅ 全數通過，見 `scripts/sandbox-smoke.py` |
| PayPal 錯誤轉譯 | ✅ `INSTRUMENT_DECLINED`、`ORDER_NOT_APPROVED` 都回乾淨的 502 + `paypal_debug_id` |
| **成功的 capture（錢真的移動）** | ❌ **未驗證**。sandbox 訪客結帳的通用測試卡（4111…、5555…）一律被拒；PayPal sandbox 要用**後台產生的專屬測試卡**或 sandbox 買家帳號，而後台被簡訊 2FA 擋住 |
| **`invoice_id` 重複阻擋** | ❌ **未驗證**，被上一項連帶擋住 —— 它只在 capture 時才會觸發。另外它取決於商家帳號的「Block accidental payments」設定，那也要進後台才看得到。**在驗證之前，不能宣稱有第二道冪等防線；DB 的 unique 約束是唯一保證** |
| 月訂閱的實際扣款與 `PAYMENT.SALE.COMPLETED` | ❌ 未驗證，同樣卡在買家授權 |

**目前 dev 用的是從舊專案 `adamhsu-apps` 借來的 sandbox 憑證**（已確認只有 sandbox 有效、
live 回 401，零真實金流風險），因為建立專屬 app 需要後台。webhook 是用 Webhooks Management API
另外註冊的（`2UN45158XR4813125`），指向本服務自己的 URL，舊服務的 webhook 沒有受影響。
**待辦：能登入後台後建立專屬的 `payment-paypal` sandbox app，並把 `PAYPAL_CLIENT_ID`
從 Secret Manager 移回 `.cicd/env.dev`。**

### 原始風險表

| 項目 | 狀態 |
|---|---|
| PayPal live 商業帳號 | ✅ 已確認可用（`adamsourceinfo@gmail.com`，live 頁面可建 app，已有兩個 live app 在跑） |
| Cloud SQL 費用 | dev / prod 各一台 db-f1-micro，都會計費。dev 不用時可 `--activation-policy=NEVER` 停機 |
| 同一商家帳號下有其他 app | sandbox 有 6 個、live 有 2 個既有 app。webhook 可能收到不屬於本服務的事件 → `caller_id IS NULL` 落地 |

### `/healthz` 不能用 —— Google Frontend 會攔截

**健康檢查路徑是 `/health` 而不是慣用的 `/healthz`，這是實測出來的必要條件。**
在 `*.run.app` 上，Google Frontend 會把 `/healthz` 自己吃掉並回自己的 404 頁面，
請求**根本不會進到容器**。證據：Cloud Run 的請求日誌裡 `/`、`/docs`、`/openapi.json`、
`/v1/orders` 全部有紀錄，只有 `/healthz` 一筆都沒有。

第一次部署就是這樣紅燈的 —— 服務其實完全健康，`/docs` 與 `/v1/*` 都正常。

### 健康檢查壞掉時必須回 503

**ci 的 smoke 只看 HTTP 狀態碼（200-399 就算過），完全不看 body。**
所以「回 200 但內容寫著 db 掛了」對 CI 來說是綠燈 —— 那這個健康檢查什麼都沒證明，
而部署一個收不了錢的服務會安靜地成功。

因此 `/health` 在 **`db.ok` 不是 true** 或 **`paypal.token` 不是 `ok`** 時回 **503**。
Cloud Run 的 startup probe 是 TCP（沒有 liveness probe），所以 503 不會讓實例被回收，
只會讓部署紅燈 —— 正是要的效果。代價：PayPal 短暫故障時部署會失敗，重跑即可。
對金流服務來說，「不能驗證就不要上線」是正確的取捨。

**`webhook: unconfigured` 刻意不算不健康** —— 那是雞生蛋的既定狀態
（要先部署拿到 URL 才註冊得了 webhook），第一次部署必須能綠燈，否則永遠部署不出去。

### 健康檢查要證明得了東西

`/health` 的 `db` 區塊裡 `server_user` 與 `database` 必須**由 DB 自己回答**，
不能是環境變數回音，否則證明不了任何事。`paypal` 區塊回報能否換到 access token，
但**不回傳 token 本身或任何憑證片段**。

```json
{ "service": "payment-paypal",
  "env": "dev",
  "version": "sha-abc1234",
  "db":      { "ok": true, "instance": "adamsourceinfo-dev:asia-east1:payment-paypal-pg",
               "server_user": "run-runtime@adamsourceinfo-dev.iam", "database": "payment_paypal" },
  "paypal":  { "env": "sandbox", "token": "ok", "webhook": "configured" } }
```

dev 與 prod 各打一次，專案名要各自對得上自己的環境。兩邊跑的是同一個 image，
差異只可能來自 CI 推導與 `.cicd/` 設定 —— 這正是重點。
