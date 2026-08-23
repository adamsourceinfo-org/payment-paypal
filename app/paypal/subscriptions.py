"""Subscriptions v1。"""
from app.paypal import client


def create_subscription(*, paypal_plan_id: str, custom_id: str,
                        subscriber_email=None, return_url=None,
                        cancel_url=None) -> dict:
    body = {"plan_id": paypal_plan_id, "custom_id": custom_id}
    if subscriber_email:
        body["subscriber"] = {"email_address": subscriber_email}
    ctx = {k: v for k, v in (("return_url", return_url),
                             ("cancel_url", cancel_url)) if v}
    if ctx:
        ctx["user_action"] = "SUBSCRIBE_NOW"
        body["application_context"] = ctx
    return client.call("POST", "/v1/billing/subscriptions", json=body)


def get_subscription(paypal_subscription_id: str) -> dict:
    return client.call(
        "GET", f"/v1/billing/subscriptions/{paypal_subscription_id}")


def cancel_subscription(paypal_subscription_id: str, reason: str = "") -> dict:
    return client.call(
        "POST", f"/v1/billing/subscriptions/{paypal_subscription_id}/cancel",
        json={"reason": reason or "cancelled by caller"})


def approve_url(sub: dict):
    for link in sub.get("links", []):
        if link.get("rel") in ("approve", "payer-action"):
            return link.get("href")
    return None
