from app import db

_COLUMNS = ("id, paypal_event_id, event_type, caller_id, subject_kind,"
            " subject_id, payload, received_at")


def record(paypal_event_id, event_type, caller_id, subject_kind, subject_id,
           payload_json: str, tx=None):
    """回新事件的 id；PayPal 重送造成的重複回 None（視為 no-op）。"""
    row = db.query(
        "INSERT INTO events (paypal_event_id, event_type, caller_id,"
        " subject_kind, subject_id, payload)"
        " VALUES (%s,%s,%s,%s,%s, %s::jsonb)"
        " ON CONFLICT (paypal_event_id) DO NOTHING RETURNING id",
        (paypal_event_id, event_type, caller_id, subject_kind, subject_id,
         payload_json), fetch="one", tx=tx)
    return row["id"] if row else None


def get(event_id, tx=None):
    """推送要拿原文來組 body；redeliver 要確認事件是不是這個 caller 的。"""
    return db.query(f"SELECT {_COLUMNS} FROM events WHERE id = %s",
                    (event_id,), fetch="one", tx=tx)


def get_by_paypal_event_id(paypal_event_id: str, tx=None):
    """PayPal 重送時，用它的全域 event id 找回我們當初落地的那一列。

    ⚠️ 去重鍵直接用 PayPal 的 `id` —— 它是全域唯一的，不像綠界要自己造。
    `dispatch.ensure()` 靠這一支把「落地了但沒排出去」補回來。
    """
    return db.query(
        f"SELECT {_COLUMNS} FROM events WHERE paypal_event_id = %s",
        (paypal_event_id,), fetch="one", tx=tx)


def list_after(caller_id: str, after: int, limit: int, tx=None):
    # caller_id IS NULL 的事件永遠不匹配任何 caller —— 這正是要的效果
    return db.query(
        "SELECT id, paypal_event_id, event_type, subject_kind, subject_id,"
        " payload, received_at FROM events"
        " WHERE caller_id = %s AND id > %s ORDER BY id LIMIT %s",
        (caller_id, after, limit), tx=tx)
