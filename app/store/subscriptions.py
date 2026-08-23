from app import db


def get_by_reference(caller_id, reference_id):
    return db.query(
        "SELECT * FROM subscriptions WHERE caller_id = %s AND reference_id = %s",
        (caller_id, reference_id), fetch="one")


def get(caller_id, sub_id):
    return db.query(
        "SELECT * FROM subscriptions WHERE caller_id = %s AND id = %s",
        (caller_id, sub_id), fetch="one")


def get_by_paypal_id(paypal_subscription_id):
    """webhook 用：由這筆資料告訴我們是哪個 caller 的。"""
    return db.query(
        "SELECT * FROM subscriptions WHERE paypal_subscription_id = %s",
        (paypal_subscription_id,), fetch="one")


def create(caller_id, plan_id, reference_id, status):
    return db.query(
        "INSERT INTO subscriptions (caller_id, plan_id, reference_id, status)"
        " VALUES (%s,%s,%s,%s) RETURNING *",
        (caller_id, plan_id, reference_id, status), fetch="one")


def attach_paypal_id(sub_id, paypal_subscription_id, status):
    return db.query(
        "UPDATE subscriptions SET paypal_subscription_id = %s, status = %s,"
        " updated_at = now() WHERE id = %s RETURNING *",
        (paypal_subscription_id, status, sub_id), fetch="one")


def set_status(sub_id, status, period_end=None):
    return db.query(
        "UPDATE subscriptions SET status = %s, updated_at = now(),"
        " current_period_end = COALESCE(%s, current_period_end)"
        " WHERE id = %s RETURNING *",
        (status, period_end, sub_id), fetch="one")


def list_(caller_id, status=None, limit=50, offset=0):
    if status:
        return db.query(
            "SELECT * FROM subscriptions WHERE caller_id = %s AND status = %s"
            " ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (caller_id, status, limit, offset))
    return db.query(
        "SELECT * FROM subscriptions WHERE caller_id = %s"
        " ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (caller_id, limit, offset))
