from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.auth import Caller, require
from app.errors import PayPalError, not_found, upstream_error
from app.models import SubscriptionCreate
from app.paypal import subscriptions as pp
from app.store import plans as plans_store
from app.store import subscriptions as store

router = APIRouter(prefix="/v1/subscriptions", tags=["subscriptions"])


def _out(row: dict, approve: Optional[str] = None) -> dict:
    d = {"id": str(row["id"]), "reference_id": row["reference_id"],
         "plan_id": str(row["plan_id"]),
         "paypal_subscription_id": row.get("paypal_subscription_id"),
         "status": row["status"],
         "current_period_end": row.get("current_period_end"),
         "created_at": row["created_at"]}
    if approve:
        d["approve_url"] = approve
    return d


@router.post("", status_code=201)
def create_subscription(body: SubscriptionCreate,
                        caller: Caller = Depends(require("subscriptions:write"))):
    # 別人的方案 → 404，不是 403
    plan = plans_store.get(caller.caller_id, body.plan_id)
    if not plan:
        raise not_found("plan")

    existing = store.get_by_reference(caller.caller_id, body.reference_id)
    if existing:
        return JSONResponse(status_code=200,
                            content=jsonable_encoder(_out(existing)))

    row = store.create(caller.caller_id, plan["id"], body.reference_id,
                       "APPROVAL_PENDING")
    try:
        sub = pp.create_subscription(
            paypal_plan_id=plan["paypal_plan_id"], custom_id=caller.caller_id,
            subscriber_email=body.subscriber_email,
            return_url=body.return_url, cancel_url=body.cancel_url)
    except PayPalError as e:
        raise upstream_error(e)

    row = store.attach_paypal_id(row["id"], sub["id"],
                                 sub.get("status", "APPROVAL_PENDING"))
    return _out(row, approve=pp.approve_url(sub))


@router.post("/{sub_id}/cancel")
def cancel(sub_id: str, caller: Caller = Depends(require("subscriptions:write"))):
    row = store.get(caller.caller_id, sub_id)
    if not row:
        raise not_found("subscription")
    try:
        pp.cancel_subscription(row["paypal_subscription_id"])
    except PayPalError as e:
        raise upstream_error(e)
    return _out(store.set_status(row["id"], "CANCELLED"))


@router.get("")
def list_subscriptions(status: Optional[str] = None,
                       limit: int = Query(default=50, ge=1, le=200),
                       offset: int = Query(default=0, ge=0),
                       caller: Caller = Depends(require("subscriptions:read"))):
    rows = store.list_(caller.caller_id, status=status, limit=limit, offset=offset)
    return {"items": [_out(r) for r in rows]}


@router.get("/{sub_id}")
def get_subscription(sub_id: str,
                     caller: Caller = Depends(require("subscriptions:read"))):
    row = store.get(caller.caller_id, sub_id)
    if not row:
        raise not_found("subscription")
    return _out(row)
