from app import db


def create(caller_id, paypal_product_id, paypal_plan_id, name, amount,
           currency, interval_count):
    return db.query(
        "INSERT INTO plans (caller_id, paypal_product_id, paypal_plan_id, name,"
        " amount, currency, interval_count) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " RETURNING *",
        (caller_id, paypal_product_id, paypal_plan_id, name, amount,
         currency, interval_count), fetch="one")


def get(caller_id, plan_id):
    return db.query("SELECT * FROM plans WHERE caller_id = %s AND id = %s",
                    (caller_id, plan_id), fetch="one")


def list_(caller_id, limit=50, offset=0):
    return db.query(
        "SELECT * FROM plans WHERE caller_id = %s"
        " ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (caller_id, limit, offset))


def set_status(plan_id, status):
    return db.query(
        "UPDATE plans SET status = %s WHERE id = %s RETURNING *",
        (status, plan_id), fetch="one")
