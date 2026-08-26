"""orders 的 SQL。每個查詢都強制帶 caller_id —— 隔離是這一層的預設值。"""
from app import db


def get_by_reference(caller_id: str, reference_id: str, tx=None):
    return db.query(
        "SELECT * FROM orders WHERE caller_id = %s AND reference_id = %s",
        (caller_id, reference_id), fetch="one", tx=tx)


def get(caller_id: str, order_id: str, tx=None):
    return db.query("SELECT * FROM orders WHERE caller_id = %s AND id = %s",
                    (caller_id, order_id), fetch="one", tx=tx)


def get_by_paypal_id(paypal_order_id: str, tx=None):
    """webhook 用：這時還不知道是哪個 caller，由這筆資料告訴我們。"""
    return db.query("SELECT * FROM orders WHERE paypal_order_id = %s",
                    (paypal_order_id,), fetch="one", tx=tx)


def create(caller_id: str, reference_id: str, amount, currency: str,
           status: str, tx=None):
    return db.query(
        "INSERT INTO orders (caller_id, reference_id, amount, currency, status)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (caller_id, reference_id, amount, currency, status), fetch="one", tx=tx)


def attach_paypal_id(order_id: str, paypal_order_id: str, status: str, tx=None):
    return db.query(
        "UPDATE orders SET paypal_order_id = %s, status = %s, updated_at = now()"
        " WHERE id = %s RETURNING *",
        (paypal_order_id, status, order_id), fetch="one", tx=tx)


def set_status(order_id: str, status: str, captured: bool = False, tx=None):
    return db.query(
        "UPDATE orders SET status = %s, updated_at = now(),"
        " captured_at = CASE WHEN %s THEN now() ELSE captured_at END"
        " WHERE id = %s RETURNING *",
        (status, captured, order_id), fetch="one", tx=tx)


def list_(caller_id: str, status=None, limit: int = 50, offset: int = 0, tx=None):
    if status:
        return db.query(
            "SELECT * FROM orders WHERE caller_id = %s AND status = %s"
            " ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (caller_id, status, limit, offset), tx=tx)
    return db.query(
        "SELECT * FROM orders WHERE caller_id = %s"
        " ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (caller_id, limit, offset), tx=tx)
