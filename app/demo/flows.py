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
