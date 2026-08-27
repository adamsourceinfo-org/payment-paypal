# payment-paypal 付款模擬頁設計

2026-08-27

## 這是什麼

一組**只在 sandbox 環境存在**的頁面，用來把兩條金流從頭到尾走一次：

- **單筆付款**：填金額 → 建單 → 跳 PayPal → 付款 → 導回 → capture
- **訂閱付款**：建/選方案 → 建訂閱 → 跳 PayPal → 訂閱 → 導回 → 等 webhook 轉 ACTIVE

以及把 `2026-08-25-webhook-delivery-design.md`（在 `payment-ecpay` 底下，兩個服務共用）
做出來的**事件推送**演給人看：事件落地之後主動推到 `/demo/sink`，
畫面上看得到「這筆推出去了沒有」。

**它是給人操作的驗證工具，不是 caller 的接入範例。** 這個區別很重要，見〈非目標〉。

## 為什麼放在 payment-paypal 裡面，而不是獨立成一個 caller

考慮過三種放法：

| 放法 | 代價 |
|---|---|
| 新 repo + 新 Cloud Run 服務 | 最乾淨（它就是個真 caller，拿自己的 API key、註冊自己的端點），但要開 repo、跑 `provision-service.sh`、設 CI |
| **塞進 payment-paypal（採用）** | 零新基礎設施、馬上可用。代價寫在下面 |
| 本機跑 | 收不到推送（PayPal 與本服務都打不到 localhost） |

**採用第二種，而代價要講清楚：這個 demo 沒辦法示範「caller 怎麼接」**——
它就是服務本人，不需要 API key、不需要跨服務的信任邊界。
所以它證明的是「**金流與推送這條路是通的**」，不是「照這樣抄就能接上」。
caller 要抄的東西在 `README.md` 的〈怎麼接事件推送〉。

## 架構

```
app/demo/__init__.py
app/demo/routes.py      /demo/* 的路由
app/demo/page.html      單一檔案，CSS/JS 內嵌
```

### 不加任何新相依

頁面是一個靜態 HTML 檔，動態部分由瀏覽器打 `/demo/api/*` 拿 JSON。
**刻意不引 Jinja2** —— 這個 repo 連 DB driver 都挑 `pg8000` 是為了不編譯，
為了一組 dev 用的頁面拉進樣板引擎不划算。

### 直接呼叫 router 函式，不繞 HTTP、不用 API key

```python
DEMO_CALLER = Caller(caller_id="demo", scopes=frozenset({...}))
orders_router.create_order(body, caller=DEMO_CALLER)
```

⚠️ **這是刻意的：不要為了 demo 再實作一次建單邏輯。**
金額驗證、冪等（同 `reference_id` 回原本那筆）、狀態機、404 語意
全部沿用同一份。手抄一份的話，那份平常不會被真流量執行 ——
而那正是最糟的一種程式碼（同樣的理由見 `app/event_view.py`）。

`caller_id = "demo"` 讓 demo 的訂單、訂閱、事件天生跟真 caller 隔離 ——
每張業務表都有 `caller_id`，而 `GET /v1/events` 的游標本來就不匹配別人的。

## prod 隔離

`app/main.py` **只在 `paypal_env == "sandbox"` 時掛上這個 router**。

⚠️ **判斷條件是 `paypal_env`，不是 `app_env`。**
真正決定「會不會動到真的錢」的是前者：prod 是 `live`，一筆 demo 訂單就是
一筆真的收款。用 `app_env` 的話，哪天有人開了第三個環境而忘了改這一行，
症狀是**在真錢上跑 demo**。綁在 `live`/`sandbox` 這個字上，錯不了。

prod 的 `/demo` 回 **404**（路由根本沒註冊），不是 403 也不是「功能已停用」頁面。

## 對外行為

| 路徑 | 做什麼 |
|---|---|
| `GET /demo` | 頁面本體 |
| `POST /demo/api/orders` | 建單，回 `approve_url` |
| `GET /demo/return/order/{id}` | PayPal 導回 → capture → 302 回 `/demo` |
| `GET /demo/cancel/order/{id}` | 使用者取消 → 302 回 `/demo` |
| `POST /demo/api/subscriptions` | 確保方案存在 + 建訂閱，回 `approve_url` |
| `GET /demo/return/subscription/{id}` | 導回 → 302 回 `/demo` |
| `GET /demo/cancel/subscription/{id}` | 同上 |
| `POST /demo/api/push/enable` | 對 caller `demo` 註冊推送端點，指向 `/demo/sink` |
| `POST /demo/sink` | **推送接收端**，驗簽後回 200 |
| `GET /demo/api/state` | 頁面輪詢：訂單、訂閱、事件、投遞紀錄 |

### 導回網址帶的是**我們自己的** id

`return_url = {base}/demo/return/order/{本地 order id}`。

⚠️ 不靠 PayPal 回傳的 `token` 去反查 —— 我們建單時就知道自己的 id，
把它放進網址是零成本的。反查要多一次查詢，而且 PayPal 在訂單與訂閱兩條路
帶回來的參數名不一樣（`token` vs `subscription_id`），統一不了。

### 訂閱不 capture

訂單要 `POST /v1/orders/{id}/capture` 才會收到錢；訂閱**沒有這一步**，
它靠 webhook `BILLING.SUBSCRIPTION.ACTIVATED` 把本地狀態轉成 `ACTIVE`。
所以訂閱的導回頁只顯示「已送出，等 PayPal 通知」，然後由輪詢看著它變 ACTIVE
—— 這正好是推送那一段要演的東西。

## 推送怎麼演

畫面上有一顆**「啟用推送」**按鈕。刻意讓它是可見的一步，因為那就是 caller 要做的事
（`PUT /v1/webhook-endpoint`）。按下去之後，caller `demo` 的推送端點指向
服務自己的 `/demo/sink`。

### `/demo/sink` 會真的驗簽

用 `signing.secret_for("demo")` 推導密鑰，照 README 給 caller 的規則檢查：

- `X-Signature` 的 `t` 與 `v1`
- `|now - t| > 300` 直接拒
- hex 比對用 `hmac.compare_digest`

驗不過回 **401**。它做的事跟一個真 caller 一模一樣，只是密鑰是本地推導、
而不是從 `PUT` 的回應複製過來。

⚠️ **驗簽一定要用原始 bytes**（`await request.body()`），不是重新序列化的 JSON。
這支端點在這裡也是一份可執行的示範 —— 它踩過就代表 caller 也會踩。

### ⚠️ sink 不存狀態，頁面從 DB 讀

Cloud Run 會有多個實例，推送打到哪一台不確定。所以 sink **不可以**把事件
存在記憶體裡等頁面來讀 —— 那在單實例的本機測試會過，上了 dev 之後
變成「有時候看得到有時候看不到」，是最難查的那一種。

`GET /demo/api/state` 一律從 DB 讀：

- `events`（`caller_id = 'demo'`）—— 事件本身，經過 `app/event_view.item()`
- `deliveries`（`caller_id = 'demo'`）—— **推送的結果**：`status` / `attempts` / `last_status`

畫面因此顯示得出「事件 #12 `PAYMENT.CAPTURE.COMPLETED` → 已推送 `delivered`（1 次嘗試）」。

**誠實的說法**：推送是真的走完整條路（Cloud Tasks → 內部端點 → 簽章 → sink），
而畫面的即時感來自**輪詢 DB**。它不是 WebSocket 推到瀏覽器，那是另一回事。

## 安全邊界

- **prod 完全沒有這些路由。**
- demo 路由**不驗 API key**。dev 服務是 `allow_unauthenticated`（PayPal 的 webhook
  必須打得到），所以任何知道網址的人都能建 sandbox 訂單。判斷是**可接受**：
  假錢、dev DB、`caller_id='demo'` 的資料不影響任何真 caller。
  真的要擋就是加一把共用密碼，很便宜，但先不做（YAGNI）。
- `/demo/sink` **要驗簽**。它是唯一一支「別人打得到而且我們會據以行動」的 demo 端點，
  而且驗簽本身就是要示範的東西。

## 測試

- **`paypal_env = "live"` 時 `/demo` 回 404** —— 這條最重要，它是 prod 隔離的全部實作
- `paypal_env = "sandbox"` 時 `/demo` 回 200 且吐得出 HTML
- sink：正確簽章 → 200；錯的 `v1` → 401；`t` 過期 → 401；沒有 `X-Signature` → 401
- sink 驗的是原始 bytes（body 含非 ASCII 時，重新序列化的簽章不該過）
- 建單：帶了正確的 `return_url`／`cancel_url`（假的 PayPal client）
- 建訂閱：沒有方案時會先建一個，已有就重用（不會每次都多一個方案）
- `GET /demo/api/state` 只回 `caller_id='demo'` 的資料
- demo 走的是 `/v1` 的 router 函式（斷言金額驗證錯誤照樣回 400 帶欄位名）

## 前置條件（人做，一次）

**PayPal sandbox 買家帳號**：developer.paypal.com → Testing Tools →
Sandbox Accounts，用 Personal 類型的帳號與密碼。沒有它，跳到 PayPal 之後
付不下去，整條路會停在 approve 頁。這一條要寫進 README。

## 非目標

| 不做 | 為什麼 |
|---|---|
| 當成 caller 的接入範例 | 它是服務本人，沒有 API key、沒有跨服務信任邊界。要抄的東西在 README 的〈怎麼接事件推送〉 |
| 商品頁／購物車 | 要驗的是金流，不是電商 |
| 多幣別 | 帳號只支援 USD（`SUPPORTED_CURRENCIES`） |
| 退款 UI | `POST /v1/orders/{id}/refund` 已經有 API，多一個 UI 只是多一份會壞的東西 |
| WebSocket／SSE 即時推到瀏覽器 | 輪詢夠用，而且多實例下 SSE 要處理黏連線。這是 dev 工具 |
| demo 路由的權限控制 | 假錢、dev、資料隔離。要的時候加一把共用密碼即可 |

## 決策紀錄

**掛載條件綁 `paypal_env` 不綁 `app_env`。** 決定「會不會動到真的錢」的是前者。
綁錯的症狀是在真錢上跑 demo。

**直接呼叫 router 函式，不重寫一份建單邏輯。** 手抄的那份平常不會被真流量執行，
而那是最糟的一種程式碼 —— 跟事件形狀只准有一份定義是同一條原則。

**sink 不存記憶體狀態。** 多實例下會變成「有時看得到」。頁面一律從 DB 讀。

**導回網址帶我們自己的 id。** 建單時就知道了，放進網址是零成本；
反查要多一次查詢，而且訂單與訂閱回傳的參數名不一樣。

**demo 不驗 API key，但 sink 驗簽。** 前者是假錢的公開玩具，
後者是「別人打得到而且我們會據以行動」的端點 —— 而且驗簽正是要示範的東西。
