"""Catalog Products + Billing Plans。PayPal 的訂閱必須先有 product 與 plan。"""
from app.money import format_amount
from app.paypal import client


def create_product(name: str, description=None) -> dict:
    body = {"name": name, "type": "SERVICE", "category": "SOFTWARE"}
    if description:
        body["description"] = description
    return client.call("POST", "/v1/catalogs/products", json=body)


def create_plan(*, product_id: str, name: str, amount, currency: str,
                interval_count: int = 1, description=None) -> dict:
    body = {
        "product_id": product_id,
        "name": name,
        "billing_cycles": [{
            "frequency": {"interval_unit": "MONTH",
                          "interval_count": interval_count},
            "tenure_type": "REGULAR",
            "sequence": 1,
            "total_cycles": 0,          # 0 = 無限期，直到取消
            "pricing_scheme": {
                "fixed_price": {"currency_code": currency,
                                "value": format_amount(amount, currency)}},
        }],
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
