# payment-paypal

以 API key 認證 caller 的 PayPal 底層後端。支援一次性訂單與月訂閱，供
`adamsourceinfo` 底下其他服務呼叫。部署走 [adamsourceinfo-org/ci](https://github.com/adamsourceinfo-org/ci)。

- 設計：[`docs/superpowers/specs/2026-08-23-payment-paypal-design.md`](docs/superpowers/specs/2026-08-23-payment-paypal-design.md)
- 實作計畫：[`docs/superpowers/plans/2026-08-23-payment-paypal.md`](docs/superpowers/plans/2026-08-23-payment-paypal.md)

## 端點

所有端點前綴 `/v1`，除 `/v1/webhooks` 與 `/health` 外都要 `X-API-Key`。
完整定義見部署後的 `/docs`（FastAPI 自動產生的 OpenAPI）。

| | 端點 | scope |
|---|---|---|
| 訂單 | `POST /v1/orders`、`/{id}/capture`、`/{id}/refund`、`GET /v1/orders[/{id}]` | `orders:read` / `orders:write` |
| 方案 | `POST /v1/plans`、`/{id}/deactivate`、`GET /v1/plans[/{id}]` | `plans:read` / `plans:write` |
| 訂閱 | `POST /v1/subscriptions`、`/{id}/cancel`、`GET /v1/subscriptions[/{id}]` | `subscriptions:read` / `subscriptions:write` |
| 事件 | `GET /v1/events?after=<cursor>&limit=100` | `events:read` |
| Webhook | `POST /v1/webhooks` | 驗 PayPal 簽章，不驗 API key |

## caller 怎麼知道訂閱扣款成功

**拉取，不是推送。** 每月扣款是 PayPal 主動發生的，本服務收到 webhook 後把事件
落地成 append-only 的 `events` 表，caller 用游標增量拉：

```
GET /v1/events?after=0            # 第一次，或對帳時從頭拉
GET /v1/events?after=<上次的 id>  # 之後每次
```

本服務**不承擔送達責任** —— 可靠推送是一整套子系統（重試、退避、死信），
caller 越多營運負擔越重，而拉取的成本落在 caller 自己身上。將來若真要推送，
`events` 表就是現成的來源。

**每月扣款成功的事件是 `PAYMENT.SALE.COMPLETED`**，不是任何 `CHECKOUT.ORDER.*`。
只看 `BILLING.SUBSCRIPTION.*` 會知道訂閱還活著，但不知道這個月的錢到了沒。

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
./scripts/add-caller.sh dev my-service "orders:read,orders:write,events:read" "備註"
```

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
