"""Orders v2。這一層只知道怎麼跟 PayPal 講話。"""
from app.money import format_amount
from app.paypal import client


def create_order(*, local_order_id: str, caller_id: str, reference_id: str,
                 amount, currency: str, description=None,
                 return_url=None, cancel_url=None) -> dict:
    """caller 歸屬要寫進 PayPal 自己的欄位。

    所有 caller 的錢都進同一個商家帳號，PayPal 的報表本身分不出哪筆是誰的。
    本地的 caller_id 是唯一的歸屬紀錄，所以在 PayPal 那邊也留一份，
    讓對帳不必只靠本服務的 DB。
    """
    unit = {
        "reference_id": local_order_id,
        "custom_id": caller_id,
        "invoice_id": f"{caller_id}:{reference_id}",
        "amount": {"currency_code": currency,
                   "value": format_amount(amount, currency)},
    }
    if description:
        unit["description"] = description

    body = {"intent": "CAPTURE", "purchase_units": [unit]}

    ctx = {k: v for k, v in (("return_url", return_url),
                             ("cancel_url", cancel_url)) if v}
    if ctx:
        ctx["user_action"] = "PAY_NOW"
        body["payment_source"] = {"paypal": {"experience_context": ctx}}

    return client.call("POST", "/v2/checkout/orders", json=body)


def capture_order(paypal_order_id: str) -> dict:
    return client.call("POST", f"/v2/checkout/orders/{paypal_order_id}/capture",
                       json={})


def get_order(paypal_order_id: str) -> dict:
    return client.call("GET", f"/v2/checkout/orders/{paypal_order_id}")


def refund_capture(capture_id: str, amount=None, currency=None, note=None) -> dict:
    body = {}
    if amount is not None and currency:
        body["amount"] = {"currency_code": currency,
                          "value": format_amount(amount, currency)}
    if note:
        body["note_to_payer"] = note
    return client.call("POST", f"/v2/payments/captures/{capture_id}/refund",
                       json=body)


def approve_url(order: dict):
    for link in order.get("links", []):
        if link.get("rel") in ("approve", "payer-action"):
            return link.get("href")
    return None


def capture_id_of(order: dict):
    for unit in order.get("purchase_units", []):
        for cap in unit.get("payments", {}).get("captures", []):
            return cap.get("id")
    return None
