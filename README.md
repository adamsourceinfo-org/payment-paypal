# payment-paypal

以 API key 認證 caller 的 PayPal 底層後端。支援一次性訂單與月訂閱，供
`adamsourceinfo` 底下其他服務呼叫。部署走 [adamsourceinfo-org/ci](https://github.com/adamsourceinfo-org/ci)。

- 設計：[`docs/superpowers/specs/2026-08-23-payment-paypal-design.md`](docs/superpowers/specs/2026-08-23-payment-paypal-design.md)
- 實作計畫：[`docs/superpowers/plans/2026-08-23-payment-paypal.md`](docs/superpowers/plans/2026-08-23-payment-paypal.md)
- 事件推送與突發韌性：設計是**兩個服務共用的一份**，寫在 `payment-ecpay` 底下
  （`docs/superpowers/specs/2026-08-26-burst-resilience-design.md` 與
  `2026-08-25-webhook-delivery-design.md`）。這裡刻意不抄第二份 ——
  抄一份就會漂移，而漂移的規格比沒有規格更糟。

## 端點

所有端點前綴 `/v1`，除 `/v1/webhooks` 與 `/health` 外都要 `X-API-Key`。
完整定義見部署後的 `/docs`（FastAPI 自動產生的 OpenAPI）。

| | 端點 | scope |
|---|---|---|
| 訂單 | `POST /v1/orders`、`/{id}/capture`、`/{id}/refund`、`GET /v1/orders[/{id}]` | `orders:read` / `orders:write` |
| 方案 | `POST /v1/plans`、`/{id}/deactivate`、`GET /v1/plans[/{id}]` | `plans:read` / `plans:write` |
| 訂閱 | `POST /v1/subscriptions`、`/{id}/cancel`、`GET /v1/subscriptions[/{id}]` | `subscriptions:read` / `subscriptions:write` |
| 事件 | `GET /v1/events?after=<cursor>&limit=100` | `events:read` |
| 推送設定 | `PUT`／`GET`／`DELETE /v1/webhook-endpoint`、`POST /v1/webhook-endpoint/test` | `webhooks:read` / `webhooks:write` |
| 投遞紀錄 | `GET /v1/deliveries?event_id=&status=&limit=`、`POST /v1/events/{id}/redeliver` | `webhooks:read` / `webhooks:write` |
| Webhook | `POST /v1/webhooks` | PayPal 打進來的**入站**接收器，驗 PayPal 簽章，不驗 API key |

## caller 怎麼知道訂閱扣款成功

事件有**兩條出口**：拉取與推送。每月扣款是 PayPal 主動發生的，本服務收到
webhook 後把事件落地成 append-only 的 `events` 表，然後：

```
GET /v1/events?after=0            # 拉取：第一次，或對帳時從頭拉
GET /v1/events?after=<上次的 id>  # 之後每次
```

```bash
# 推送：註冊一次，之後事件落地就主動 POST 給你
curl -s -X PUT "$BASE/v1/webhook-endpoint" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://your-service.a.run.app/pay/events"}'
```

**每月扣款成功的事件是 `PAYMENT.SALE.COMPLETED`**，不是任何 `CHECKOUT.ORDER.*`。
只看 `BILLING.SUBSCRIPTION.*` 會知道訂閱還活著，但不知道這個月的錢到了沒。

### 這裡原本寫著「本服務不承擔送達責任」

那句話（連同設計決策 3）**在 2026-08-26 被推翻了**。原本的理由是：

> 可靠推送是一整套子系統（重試、退避、死信），caller 越多營運負擔越重，
> 而拉取的成本落在 caller 自己身上。

推翻它的是兩件當時沒想到的事：

**一、caller 也 scale to zero，「拉取的成本落在 caller 自己身上」不成立。**
caller 跑在 Cloud Run，沒有流量時沒有任何 process 在跑，也就沒有人去拉。
使用者剛付完款那一刻沒問題（他自己的請求就會觸發拉取），但**每月續期扣款**
那筆錢進來時，沒有任何人在跟 caller 講話。要修掉那個延遲，每一個 caller
都得自己開一個 Cloud Scheduler、管一把密鑰、維護一支只為了叫醒自己而存在的
端點 —— 那正是原決策想避免的負擔，只是跑到了另一邊。

**二、那「一整套子系統」現在租得到。** Cloud Tasks 就是重試、指數退避、放棄
那一整套，而且不需要常駐 process，跟 scale-to-zero 天生共存。

原句的最後一句話仍然成立，而且正是新設計的起點：「`events` 表就是現成的來源。」

**但責任邊界沒有變得無限**，只是換了個位置，寫清楚：
服務保證的是「**盡力送到，而且送不到你查得出來**」（`GET /v1/deliveries`），
不是「保證送達」—— 重試是有界的（12 小時），用完會標成 `dead`。
所以**拉取端點永遠保留，而且它是推送的安全網**。詳見〈怎麼接事件推送〉。

服務的責任也只到「通知 caller」為止。caller 拿到通知之後要推 LINE、開 SSE、
還是讓前端輪詢，是 caller 的設計空間。


## 怎麼接事件推送

推送的用途是「**沒有人在跟你的服務講話的時候**」—— 典型的是每月續期扣款。
如果你的服務也 scale to zero，那筆錢進來時沒有任何請求會觸發你去拉。

沒註冊端點的 caller 完全不會有推送，`GET /v1/events` 一個位元組都沒變。

### 一、註冊

```bash
curl -s -X PUT "$BASE/v1/webhook-endpoint" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://your-service.a.run.app/pay/events"}'
```

回應帶 `secret` —— 那是你的簽章密鑰。它是**推導**出來的，不是隨機產生存起來的，
所以 `GET /v1/webhook-endpoint` 隨時拿得回同一把，不存在「弄丟了」這條路。

只收 `https://`。內網位址（`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、
loopback）與 `.internal` 一律回 400。

### 二、驗簽（TypeScript）

```ts
import { createHmac, timingSafeEqual } from 'node:crypto';

const TOLERANCE_SECONDS = 300;

export function verify(
  rawBody: Buffer, header: string, secret: string, now: Date,
): boolean {
  // 不要用 split('=', 2) —— JS 的第二個參數是「取幾段」不是「切幾次」，
  // 值裡真的出現 '=' 時它會把後半截默默丟掉。
  const parts: Record<string, string> = {};
  for (const kv of header.split(',')) {
    const i = kv.indexOf('=');
    if (i > 0) parts[kv.slice(0, i).trim()] = kv.slice(i + 1);
  }
  const t = Number(parts.t);
  if (!Number.isFinite(t)) return false;
  if (Math.abs(now.getTime() / 1000 - t) > TOLERANCE_SECONDS) return false;

  const expected = createHmac('sha256', secret)
    .update(`${t}.`).update(rawBody).digest();
  const got = Buffer.from(parts.v1 ?? '', 'hex');
  return got.length === expected.length && timingSafeEqual(got, expected);
}
```

⚠️ **驗簽必須用原始 bytes。** Express 預設會先把 JSON 解析掉，而重新
`JSON.stringify` 出來的字串跟原文**不保證逐位元組相同**（鍵的順序、Unicode
跳脫、空白都可能不同）。接收端要用 `express.raw({ type: 'application/json' })`。

這個 bug 只有在 payload 含非 ASCII 時才發作 —— 而 PayPal 的 payer 姓名可以是中文。

**簽章向量**（`payment-paypal` 與 `payment-ecpay` **算出來必須逐字相同**，
拿它驗你自己的實作。只有這一組，不要各自產生「應該相同」的向量）：

```
WEBHOOK_SIGNING_KEY = "test-signing-key"
caller_id           = "line-translate-bot"
secret              = a6b1f5b99eceb78d8161ce309c2aaa884331bfae5d0f0b438458795953a38a4c

t    = 1756090455
body = {"id":1234,"event_type":"payment.return"}
X-Signature = t=1756090455,v1=5b1967f64135c6dff853b169effe4421cf9a1e0dff72125008c789f3d4bd2b39
```

### 三、body 與 header

body 就是 `GET /v1/events` 回應裡 `items[]` 的**一個元素**，逐欄相同 ——
所以你只要寫一份 parser，兩條路都能吃。

```json
{
  "id": 1234,
  "event_type": "PAYMENT.SALE.COMPLETED",
  "subject_kind": "subscription",
  "subject_id": "0f9c1a2b-…",
  "payload": { "…PayPal 原文…": true },
  "received_at": "2026-08-26T03:14:15.926Z"
}
```

`payload` 是 PayPal 送來的**原文**，服務不做解讀。

| Header | 說明 |
|---|---|
| `X-Signature` | `t=<unix秒>,v1=<小寫 hex>` |
| `X-Event-Id` | 事件 id。**不可信**，別拿它當去重鍵 |
| `X-Event-Type` | 同 body |
| `X-Delivery-Id` | 拿去 `GET /v1/deliveries` 查案 |
| `X-Delivery-Attempt` | 第幾次嘗試，從 1 起算 |

### 四、你必須處理的四件事

**1. 用 body 裡的 `id` 去重。** 投遞是**至少一次**、**不保證順序**。
`id` 是 `bigserial`，天然單調。用 `X-Event-Id` 去重是錯的 —— 它沒有經過驗簽。

**2. `event_type === "ping"` 要在去重之前就 return。**
ping 的 `id` 固定是 `0`，照順序去重的話第二次 ping 會被你自己擋掉，
看起來像沒送到。

**3. `BILLING.SUBSCRIPTION.*` 不代表收到錢。**
訂閱還活著跟這個月的錢到了沒是兩件事。收到錢的是
`PAYMENT.SALE.COMPLETED`（月訂閱）與 `PAYMENT.CAPTURE.COMPLETED`（一次性訂單）。

**4. 剛註冊完會收到一批過去 48 小時的事件。**
補漏機制只看「有沒有投遞紀錄」，不看「事件落地當下你註冊了沒有」。
這是刻意的 —— 接上推送之前那兩天的續期扣款不會憑空消失。用 `id` 去重就好。

### 五、回什麼

- **2xx** = 收下了，不再重送
- **其他任何回應（含 timeout）** = 我們會重試，最長 12 小時、約 23 次，
  指數退避到一小時封頂

處理失敗時**回 500 讓我們重送** —— 純拉取沒有這個機制（游標一推進就回不去了），
推送把那個安全網還給你，而且由你控制。

### 六、送不到的時候

```bash
# 那筆到底送出去沒有
curl -s "$BASE/v1/deliveries?event_id=1234" -H "X-API-Key: $KEY"

# 修好接收端之後補送
curl -s -X POST "$BASE/v1/events/1234/redeliver" -H "X-API-Key: $KEY"

# 沒有任何真實金流也能驗完整條路
curl -s -X POST "$BASE/v1/webhook-endpoint/test" -H "X-API-Key: $KEY"
```

重試用完仍失敗的會標成 `dead` 並留在 `GET /v1/deliveries` 裡 ——
**我們不會自動停用你的端點**，那是營運決策，不替你做。

⚠️ **改網址不會讓已經排進佇列的投遞改道。**
每一列投遞記的是**排程當下**的網址（這樣「這筆當初送去哪」才答得出來），
所以換了網址之後，還在重試的那些會繼續打舊網址直到重試用完。
要立刻改道就 `redeliver` —— 新建的那一列會用新網址。

⚠️ **拉取端點永遠保留，而且它是推送的安全網。**
推送有界的重試不等於保證送達。跑一支低頻對帳拉取
（例如每天一次 `GET /v1/events?after=<你的游標>`）永遠是對的 ——
那是唯一一層不依賴我們的。

### 密鑰輪替的代價

簽章密鑰由服務端的一把主金鑰推導。**換掉它等於同時換掉所有 caller 的密鑰。**
這是刻意的取捨 —— 逐 caller 輪替換來的是資料庫裡多一欄要保護的明文。
真的要輪替就是一次全部，而且會事先通知。

## 突發流量下會怎樣

前提是多個 caller、而且通常是行銷活動 —— 大量使用者集中在某個時刻付款。
為此做了四件事（設計見 `payment-ecpay` 的突發韌性文件）：

- **入站 webhook 不佔用事件迴圈。** `POST /v1/webhooks` 是全服務唯一的
  `async def`，而它裡面有驗簽的對外 HTTP、同步的 pg8000、建 Cloud Task 的
  對外 HTTP。所以最外層只 `await request.body()`，其餘丟 threadpool ——
  否則一筆 webhook 就是幾百毫秒的**全實例**停擺，包括 caller 正在查的
  `GET /v1/orders/{id}`。
- **`DB_POOL_MAX` 是同時在外的連線數硬上限**，不只是池的大小。
  `apps-pg` 是一個環境一台、服務只靠 database 分隔 ——
  連線耗盡會把同一台上的**其他服務**一起拖下水。借不到就等
  `DB_POOL_TIMEOUT_SECONDS`，逾時回 503（不是無限等：無限等會讓症狀
  從「慢」變成「整個實例沒反應」，健康檢查也跟著死）。
  容量規則：**實例數 × `DB_POOL_MAX` ≤ 本服務的 Cloud SQL 連線預算**，
  `max-instances` 與 `DB_POOL_MAX` 要一起看。
- **一個 caller 一個 Cloud Tasks queue。** `max-concurrent-dispatches` 是
  每個 queue 的設定 —— 共用的話，一個 caller 的端點 timeout 10 秒就能佔滿
  全部派送槽位，排隊擋住其他所有 caller。queue 在 `add-caller.sh` 建。
- **冷啟動不排隊。** migration 用 `pg_try_advisory_lock`，拿不到就跳過
  （代表別的實例正在跑）。阻塞式的鎖會讓「20 個實例同時冷啟動」變成序列的。

## 幾個刻意的決定

- **幣別只支援 USD**（帳號限制），且**沒有預設值** —— 忘了傳幣別要是明確的錯誤，不是靜默通過。
  小數位數是幣別屬性（`{"USD": 2, "TWD": 0}`），不寫死 2。
- **金額一律正規化成幣別位數**：`"7"` → `"7.00"`、`"12.5"` → `"12.50"`；
  超過位數回 400 並**指名欄位**：`{"error":"invalid_amount","field":"amount","message":…}`。
  收金額的端點有三個 —— `POST /v1/plans`、`POST /v1/orders`、`POST /v1/orders/{id}/refund`。
  **`POST /v1/subscriptions` 沒有金額欄位**，月訂閱的金額在建方案時就決定了。
- **PayPal base URL 由 `PAYPAL_ENV` 推導**，不做成設定。可設定就有設錯的餘地，
  而「prod 指到 sandbox」的代價是以為在收錢但沒有。
- **一環境一組 Client ID，不是每 caller 一組。** Client ID 認證的是「錢進哪個商家帳號」，
  跟 caller 無關 —— PayPal 的模型裡不存在 per-caller 憑證。
- **沒有 admin API、沒有萬能鑰匙。** API key 用 `scripts/add-caller.sh` 手動建立。
- **查別人的資源回 404 不是 403**（403 會洩漏「該資源存在」）。
- **重複的 `reference_id` 回 200 + 原本那筆** —— 冪等不是錯誤。
- **推送不取代拉取，是第二條出口。** 兩邊送的是**同一個形狀**（`app/event_view.py`
  的 `item()` 定義一次，兩條出口都用它）—— caller 寫一份 parser。
- **簽章密鑰不入庫，由 `WEBHOOK_SIGNING_KEY` 推導。** API key 是入站認證，
  只需要*驗證*所以存 sha256 就夠；簽章密鑰是出站的，服務必須*持有*它才簽得出來
  —— 存 hash 沒有意義，存明文就是資料庫裡多一欄機密。
- **死信的門檻向 queue 本人問**（`retryConfig.maxRetryDuration` 的兩倍），
  程式裡沒有第二個 max-attempts。留一個 env 就是留第二份真相，
  而沒有東西在守它們一致 —— 症狀會是「死信永遠不會被標記」。
- **`/health` 報告積壓的死信，但不因此回 503。** 死信通常代表 caller 壞了，
  用 503 表達會讓 CI 的 smoke 從「我們部署成功了嗎」變成「所有 caller 今天都好嗎」。

## 本機開發

```bash
uv venv --python python3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
```

**要用 Python 3.12**（與容器一致）。3.14 上 `pydantic-core` 沒有 wheel，會嘗試編譯而失敗。

CI 不跑測試，測試在本機驗過才推。

## 新增一個 caller

```bash
./scripts/add-caller.sh dev my-service \
  "orders:read,orders:write,events:read,webhooks:read,webhooks:write" "備註"
```

它同時會建這個 caller 專屬的 Cloud Tasks queue（推送的公平性隔離）。
⚠️ 建 queue 排在寫 `api_keys` **之前** —— 反過來的話 `set -e` 會讓腳本在
印出明文金鑰**之前**就死，DB 裡多一把沒有人知道值的 key。

**已經上線的 caller 要補 scope** 用的是另一支（`add-caller.sh` 只能新增不能改，
重發一把新 key 等於逼 caller 改設定重新部署）：

```bash
./scripts/grant-scope.sh dev my-service "webhooks:read,webhooks:write"
```

它是**附加**不是覆寫，而且會印出「原本／現在／新增幾個」讓人肉眼確認。

**不需要密碼。** 人跟服務走同一個機制：Cloud SQL IAM 認證，拿自己的 access token 當密碼。
整套設計裡不存在 DB 密碼這種東西。

每個環境一次性前置（把自己加成 IAM 使用者、並取得 app 建的表的權限）：

```bash
ME=$(gcloud config get-value account); PROJECT=adamsourceinfo-dev
gcloud sql users create "$ME" --instance=apps-pg \
  --project="$PROJECT" --type=cloud_iam_user
# 表的擁有者是 run-runtime（app 的 migration 建的），要成為該角色的成員才存取得到
psql -h 127.0.0.1 -p 5433 -U "$ME" -d payment_paypal \
  -c 'GRANT "run-payment-paypal@'"$PROJECT"'.iam" TO "'"$ME"'"'
```

明文 key 只會顯示一次，DB 裡只有 sha256。

## 開通與部署

見 spec 的「一次性 runbook」。順序不可顛倒 —— **webhook 有雞生蛋**：
第一次部署才拿得到 Cloud Run URL，有 URL 才能去 PayPal 註冊 webhook，
註冊後才有 `PAYPAL_WEBHOOK_ID` 可以填回 `.cicd/env.<env>`，然後再部署一次。

`PAYPAL_WEBHOOK_ID` 缺席時服務**正常啟動但降級**：`/health` 回
`"webhook": "unconfigured"`，`POST /v1/webhooks` 回 503 —— 不會靜靜收下無法驗簽的請求。

還要手動設 `--max-instances`：db-f1-micro 的 `max_connections` 約 25，
Cloud Run 預設可擴到 100 實例 × `DB_POOL_MAX=3` 會打爆連線數。
CI 從不傳 min/max instances，所以手動設定會被保留。
⚠️ 這個數字與 `DB_POOL_MAX` 要一起看，任一邊單獨調都是錯的。

### 推送的一次性 runbook（人跑，每個環境一次）

```bash
ENV=dev; PROJECT=adamsourceinfo-${ENV}; SVC=payment-paypal; REGION=asia-east1
URL="$(gcloud run services describe "$SVC" --region="$REGION" \
        --project="$PROJECT" --format='value(status.url)')"

# 1. 開 API
gcloud services enable cloudtasks.googleapis.com cloudscheduler.googleapis.com \
  --project="$PROJECT"

# 2. 兩把機密
#    ⚠️ **不要用 `print()`** —— 它會把換行也存進 secret，而 Cloud Run 是原樣注入的。
#    症狀：INTERNAL_KEY 變成 "abc\n"，內部端點永遠回 401（比對的另一邊是
#    shell 展開時 trim 過的）。app/config.py 的 _optional() 已經會 strip，
#    但建的時候就別放進去。
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' | \
  gcloud secrets create "${SVC}-webhook-signing-key-${ENV}" \
    --replication-policy=automatic --data-file=- --project="$PROJECT"
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' | \
  gcloud secrets create "${SVC}-internal-key-${ENV}" \
    --replication-policy=automatic --data-file=- --project="$PROJECT"

# 3. 授權給**這個服務的**執行身分（run-payment-paypal，不是共用的 run-runtime）
for S in "${SVC}-webhook-signing-key-${ENV}" "${SVC}-internal-key-${ENV}"; do
  gcloud secrets add-iam-policy-binding "$S" --project="$PROJECT" \
    --member="serviceAccount:run-${SVC}@${PROJECT}.iam.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done

# ⚠️ 第 2、3 步要排在 push 之前 —— .cicd/secrets.<env> 引用一個不存在的
#    secret 會讓新 revision 直接起不來。

# 4. 共用的退路 queue（per-caller 的那些由 add-caller.sh 建）
# ⚠️ 時長只收**秒**：43200s 不能寫成 12h、3600s 不能寫成 1h，
#    API 會回「Illegal duration format; duration must end with 's'」。
#    這是 Cloud Tasks API 的驗證不是 gcloud 的，所以只有實跑才會發現。
gcloud tasks queues create "${SVC}-deliveries" --location="$REGION" \
  --project="$PROJECT" \
  --max-retry-duration=43200s --max-attempts=30 \
  --min-backoff=10s --max-backoff=3600s --max-doublings=5 \
  --max-concurrent-dispatches=10

# 5. 讓執行身分排得進 task、也讀得到 queue 設定（viewer 是 sweep 判死信要的）
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:run-${SVC}@${PROJECT}.iam.gserviceaccount.com" \
  --role=roles/cloudtasks.enqueuer --condition=None
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:run-${SVC}@${PROJECT}.iam.gserviceaccount.com" \
  --role=roles/cloudtasks.viewer --condition=None

# 6. Scheduler：每小時掃一次
#    ⚠️ INTERNAL_KEY 會出現在 job 設定裡，任何有 scheduler.viewer 的人看得到。
#    它靠專案的 IAM 保護，不是靠對專案成員保密 —— 跟 Cloud Task 的 header 同一個模型。
KEY="$(gcloud secrets versions access latest \
        --secret="${SVC}-internal-key-${ENV}" --project="$PROJECT")"
gcloud scheduler jobs create http "${SVC}-deliveries-sweep" \
  --location="$REGION" --project="$PROJECT" \
  --schedule="23 * * * *" --time-zone=Asia/Taipei \
  --uri="${URL}/internal/deliveries/sweep" --http-method=POST \
  --headers="X-Internal-Key=${KEY}" \
  --attempt-deadline=300s

# 7. 既有 caller 補 scope（新 scope 不會自己長出來）
./scripts/grant-scope.sh "$ENV" <caller> "webhooks:read,webhooks:write"

# 8. 複查 —— 併發下 add-iam-policy-binding 可能靜靜掉一筆
gcloud projects get-iam-policy "$PROJECT" --flatten="bindings[].members" \
  --filter="bindings.members:run-${SVC}@" --format="value(bindings.role)"
```
