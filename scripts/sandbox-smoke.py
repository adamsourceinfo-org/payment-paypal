#!/usr/bin/env python3
"""對已部署的服務跑真實 sandbox 實測。

跟單元測試互補：這裡不 mock 任何東西，PayPal 是真的、DB 是真的。
用法：
    BASE_URL=https://... KEY_A=... KEY_B=... python3 scripts/sandbox-smoke.py
KEY_A 要有全部 scope，KEY_B 只有 orders:*（用來測隔離與 scope 不足）。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ["BASE_URL"].rstrip("/")
KEY_A = os.environ["KEY_A"]
KEY_B = os.environ["KEY_B"]

results = []


def call(method, path, key=None, body=None, raw=None, headers=None):
    url = f"{BASE}{path}"
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        try:
            return e.code, json.loads(raw_body or b"{}")
        except Exception:
            return e.code, {"_raw": raw_body.decode(errors="replace")[:200]}


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


ref = lambda p: f"{p}-{int(time.time())}"

print("\n[1] 健康檢查 —— db 的身分必須由 DB 自己回答")
s, h = call("GET", "/health")
check("health 200", s == 200)
check("db.ok", h.get("db", {}).get("ok") is True)
check("server_user 由 DB 回答", h["db"].get("server_user") == "run-runtime@adamsourceinfo-dev.iam",
      h["db"].get("server_user", ""))
check("database 由 DB 回答", h["db"].get("database") == "payment_paypal")
check("paypal token ok", h["paypal"]["token"] == "ok")
check("webhook configured", h["paypal"]["webhook"] == "configured")
check("回應不含憑證片段", "BAA" not in json.dumps(h))

print("\n[2] 認證與授權")
s, _ = call("GET", "/v1/orders")
check("沒帶 key → 401", s == 401)
s, _ = call("GET", "/v1/orders", key="garbage")
check("錯誤 key → 401", s == 401)
s, b = call("GET", "/v1/plans", key=KEY_B)
check("scope 不足 → 403", s == 403, str(b.get("detail", ""))[:60])
s, _ = call("GET", "/v1/orders", key=KEY_A)
check("有效 key → 200", s == 200)

print("\n[3] 訂單：幣別、冪等、歸屬")
s, b = call("POST", "/v1/orders", key=KEY_A,
            body={"reference_id": ref("twd"), "amount": "300", "currency": "TWD"})
check("非 USD → 400", s == 400, str(b.get("detail", ""))[:60])
s, b = call("POST", "/v1/orders", key=KEY_A,
            body={"reference_id": ref("dec"), "amount": "10.000", "currency": "USD"})
check("USD 三位小數 → 400", s == 400)

R1 = ref("ord")
s, o1 = call("POST", "/v1/orders", key=KEY_A,
             body={"reference_id": R1, "amount": "10.00", "currency": "USD",
                   "description": "sandbox smoke"})
check("建單 → 201", s == 201, str(o1)[:80])
check("回傳 approve_url", str(o1.get("approve_url", "")).startswith("https://"))
check("回傳 paypal_order_id", bool(o1.get("paypal_order_id")))
ORDER_ID, PP_ORDER = o1.get("id"), o1.get("paypal_order_id")

s, o2 = call("POST", "/v1/orders", key=KEY_A,
             body={"reference_id": R1, "amount": "10.00", "currency": "USD"})
check("重複 reference_id → 200 且同一筆", s == 200 and o2.get("id") == ORDER_ID)

s, b = call("GET", f"/v1/orders/{ORDER_ID}", key=KEY_B)
check("別的 caller 查 → 404（不是 403）", s == 404)

s, b = call("GET", "/v1/orders", key=KEY_B)
check("別的 caller 列表看不到", all(i["id"] != ORDER_ID for i in b.get("items", [])))

print("\n[4] 未授權就 capture → 乾淨的 502 + PayPal debug_id")
s, b = call("POST", f"/v1/orders/{ORDER_ID}/capture", key=KEY_A)
d = b.get("detail", {}) if isinstance(b.get("detail"), dict) else {}
check("capture 未授權訂單 → 502", s == 502, str(b)[:70])
check("帶回 paypal_debug_id", bool(d.get("paypal_debug_id")), str(d.get("paypal_debug_id", "")))

print("\n[5] 方案與訂閱（USD 月費）")
s, p = call("POST", "/v1/plans", key=KEY_A,
            body={"name": "Smoke Basic", "amount": "9.99", "currency": "USD"})
check("建方案 → 201", s == 201, str(p)[:80])
PLAN_ID = p.get("id")
check("PayPal plan id 存在", bool(p.get("paypal_plan_id")))
s, b = call("POST", "/v1/plans", key=KEY_A,
            body={"name": "x", "amount": "300", "currency": "TWD"})
check("方案非 USD → 400", s == 400)

RS = ref("sub")
s, sub = call("POST", "/v1/subscriptions", key=KEY_A,
              body={"reference_id": RS, "plan_id": PLAN_ID})
check("建訂閱 → 201", s == 201, str(sub)[:80])
check("訂閱回 approve_url", str(sub.get("approve_url", "")).startswith("https://"))
check("狀態 APPROVAL_PENDING", sub.get("status") == "APPROVAL_PENDING")
SUB_ID = sub.get("id")
s, sub2 = call("POST", "/v1/subscriptions", key=KEY_A,
               body={"reference_id": RS, "plan_id": PLAN_ID})
check("訂閱冪等 → 200 同一筆", s == 200 and sub2.get("id") == SUB_ID)

s, _ = call("POST", "/v1/subscriptions", key=KEY_B,
            body={"reference_id": ref("x"), "plan_id": PLAN_ID})
check("訂別人的方案 → 401/404", s in (401, 403, 404), f"HTTP {s}")

print("\n[6] Webhook 端點")
s, b = call("POST", "/v1/webhooks", raw=b'{"id":"fake","event_type":"X"}')
check("無效簽章 → 401", s == 401, str(b)[:60])

print("\n[7] 事件流")
s, b = call("GET", "/v1/events?after=0", key=KEY_A)
check("events 可讀 → 200", s == 200)
check("有 next_cursor", "next_cursor" in b)
s, _ = call("GET", "/v1/events?after=0", key=KEY_B)
check("沒有 events:read 的 caller → 403", s == 403)
s, _ = call("GET", "/v1/events?after=0&limit=501", key=KEY_A)
check("limit 超過上限 → 422", s == 422)

print("\n[8] 金額正規化與欄位級錯誤")
s, b = call("POST", "/v1/orders", key=KEY_A,
            body={"reference_id": ref("int"), "amount": "7", "currency": "USD"})
check("整數金額補成 .00", s == 201 and b.get("amount") == "7.00", str(b.get("amount")))
s, b = call("POST", "/v1/orders", key=KEY_A,
            body={"reference_id": ref("one"), "amount": "12.5", "currency": "USD"})
check("一位小數補成兩位", s == 201 and b.get("amount") == "12.50", str(b.get("amount")))
s, b = call("POST", "/v1/orders", key=KEY_A,
            body={"reference_id": ref("bad"), "amount": "10.001", "currency": "USD"})
d = b.get("detail", {}) if isinstance(b.get("detail"), dict) else {}
check("三位小數 → 400 且指名 amount 欄位",
      s == 400 and d.get("field") == "amount" and d.get("error") == "invalid_amount", str(d))
s, b = call("POST", "/v1/orders", key=KEY_A,
            body={"reference_id": ref("cur"), "amount": "10.00", "currency": "EUR"})
d = b.get("detail", {}) if isinstance(b.get("detail"), dict) else {}
check("不支援幣別 → 400 且指名 currency 欄位",
      s == 400 and d.get("field") == "currency", str(d))

# 方案也走同一套規則（訂閱本身不收金額，所以限制在方案這一層）
s, b = call("POST", "/v1/plans", key=KEY_A,
            body={"name": f"norm {ref('p')}", "amount": "15", "currency": "USD"})
check("方案整數金額補成 .00", s == 201 and b.get("amount") == "15.00", str(b.get("amount")))
s, b = call("POST", "/v1/plans", key=KEY_A,
            body={"name": "bad", "amount": "9.999", "currency": "USD"})
d = b.get("detail", {}) if isinstance(b.get("detail"), dict) else {}
check("方案三位小數 → 400 指名 amount", s == 400 and d.get("field") == "amount", str(d))

print("\n[9] 健康檢查在壞掉時要能讓 CI 擋下來")
check("健康時回 200（ci smoke 只看狀態碼）", call("GET", "/health")[0] == 200)

failed = [n for n, ok, _ in results if not ok]
print(f"\n{'='*60}\n通過 {len(results)-len(failed)}/{len(results)}")
if failed:
    print("失敗：")
    for n in failed:
        print("  -", n)
    sys.exit(1)
print("全部通過")
