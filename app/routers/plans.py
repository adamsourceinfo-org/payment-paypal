from fastapi import APIRouter, Depends, Query

from app.auth import Caller, require
from app.errors import (InvalidAmount, PayPalError, UnsupportedCurrency,
                        bad_request, not_found, upstream_error)
from app.models import PlanCreate
from app.money import format_amount, validate_amount
from app.paypal import plans as pp
from app.store import plans as store

router = APIRouter(prefix="/v1/plans", tags=["plans"])


def _out(row: dict) -> dict:
    return {"id": str(row["id"]), "name": row["name"],
            "amount": format_amount(row["amount"], row["currency"]), "currency": row["currency"],
            "interval_unit": row["interval_unit"],
            "interval_count": row["interval_count"],
            "status": row["status"],
            "paypal_plan_id": row["paypal_plan_id"],
            "created_at": row["created_at"]}


@router.post("", status_code=201)
def create_plan(body: PlanCreate,
                caller: Caller = Depends(require("plans:write"))):
    try:
        amount = validate_amount(body.amount, body.currency)
    except (UnsupportedCurrency, InvalidAmount) as e:
        raise bad_request(e)
    currency = body.currency.upper()

    try:
        # 兩步：先 product 再 plan。PayPal 要求 plan 必須掛在 product 底下。
        product = pp.create_product(body.name, body.description)
        plan = pp.create_plan(product_id=product["id"], name=body.name,
                              amount=amount, currency=currency,
                              interval_count=body.interval_count,
                              description=body.description)
    except PayPalError as e:
        raise upstream_error(e)

    row = store.create(caller.caller_id, product["id"], plan["id"], body.name,
                       amount, currency, body.interval_count)
    return _out(row)


@router.get("")
def list_plans(limit: int = Query(default=50, ge=1, le=200),
               offset: int = Query(default=0, ge=0),
               caller: Caller = Depends(require("plans:read"))):
    return {"items": [_out(r) for r in
                      store.list_(caller.caller_id, limit=limit, offset=offset)]}


@router.get("/{plan_id}")
def get_plan(plan_id: str, caller: Caller = Depends(require("plans:read"))):
    row = store.get(caller.caller_id, plan_id)
    if not row:
        raise not_found("plan")
    return _out(row)


@router.post("/{plan_id}/deactivate")
def deactivate(plan_id: str, caller: Caller = Depends(require("plans:write"))):
    row = store.get(caller.caller_id, plan_id)
    if not row:
        raise not_found("plan")
    try:
        pp.deactivate_plan(row["paypal_plan_id"])
    except PayPalError as e:
        raise upstream_error(e)
    return _out(store.set_status(row["id"], "INACTIVE"))
