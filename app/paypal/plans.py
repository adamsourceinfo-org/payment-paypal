"""Catalog Products + Billing Plans。PayPal 的訂閱必須先有 product 與 plan。"""
from app.money import format_amount
from app.paypal import client


def create_product(name: str, description=None) -> dict:
    body = {"name": name, "type": "SERVICE", "category": "SOFTWARE"}
    if description:
        body["description"] = description
    return client.call("POST", "/v1/catalogs/products", json=body)


def create_plan(*, product_id: str, name: str, amount, currency: str,
                interval_count: int = 1, description=None,
                trial_weeks: int = 0) -> dict:
    cycles = []
    if trial_weeks > 0:
        # 免費試用是排在 REGULAR 前面的一個 TRIAL 週期。
        # 單位用 WEEK（PayPal 文件的試用範例就是這樣寫），interval_count 就是週數、
        # total_cycles 固定 1 —— 四週是「一個」四週長的週期，不是四個一週的週期。
        # 價格明寫 0：PayPal 對 TRIAL 一樣要求 pricing_scheme，省略不等於免費。
        cycles.append({
            "frequency": {"interval_unit": "WEEK",
                          "interval_count": trial_weeks},
            "tenure_type": "TRIAL",
            "sequence": 1,
            "total_cycles": 1,
            "pricing_scheme": {
                "fixed_price": {"currency_code": currency, "value": "0"}},
        })
    cycles.append({
        "frequency": {"interval_unit": "MONTH",
                      "interval_count": interval_count},
        "tenure_type": "REGULAR",
        "sequence": len(cycles) + 1,    # 沒有試用就是 1，跟以前一模一樣
        "total_cycles": 0,              # 0 = 無限期，直到取消
        "pricing_scheme": {
            "fixed_price": {"currency_code": currency,
                            "value": format_amount(amount, currency)}},
    })
    body = {
        "product_id": product_id,
        "name": name,
        "billing_cycles": cycles,
        "payment_preferences": {"auto_bill_outstanding": True,
                                "setup_fee_failure_action": "CONTINUE",
                                "payment_failure_threshold": 3},
    }
    if description:
        body["description"] = description
    return client.call("POST", "/v1/billing/plans", json=body)


def deactivate_plan(paypal_plan_id: str) -> dict:
    return client.call("POST", f"/v1/billing/plans/{paypal_plan_id}/deactivate",
                       json={})
