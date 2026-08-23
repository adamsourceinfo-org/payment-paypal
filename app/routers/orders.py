from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.auth import Caller, require
from app.errors import (InvalidAmount, PayPalError, UnsupportedCurrency,
                        bad_request, not_found, upstream_error)
from app.models import OrderCreate, RefundCreate
from app.money import format_amount, validate_amount
from app.paypal import orders as pp
from app.store import orders as store

router = APIRouter(prefix="/v1/orders", tags=["orders"])


def _out(row: dict, approve: Optional[str] = None) -> dict:
    d = {
        "id": str(row["id"]),
        "reference_id": row["reference_id"],
        "paypal_order_id": row.get("paypal_order_id"),
        "amount": format_amount(row["amount"], row["currency"]),
        "currency": row["currency"],
        "status": row["status"],
        "created_at": row["created_at"],
    }
    if approve:
        d["approve_url"] = approve
    return d


@router.post("", status_code=201)
def create_order(body: OrderCreate,
                 caller: Caller = Depends(require("orders:write"))):
    try:
        amount = validate_amount(body.amount, body.currency)
    except (UnsupportedCurrency, InvalidAmount) as e:
        # 擋在進門處，不浪費一次 PayPal 呼叫
        raise bad_request(e)
    currency = body.currency.upper()

    existing = store.get_by_reference(caller.caller_id, body.reference_id)
    if existing:
        # 冪等：重複的 reference_id 不是錯誤，回原本那筆
        return JSONResponse(status_code=200,
                            content=jsonable_encoder(_out(existing)))

    row = store.create(caller.caller_id, body.reference_id, amount,
                       currency, "PENDING")
    try:
        pp_order = pp.create_order(
            local_order_id=str(row["id"]), caller_id=caller.caller_id,
            reference_id=body.reference_id, amount=amount, currency=currency,
            description=body.description, return_url=body.return_url,
            cancel_url=body.cancel_url)
    except PayPalError as e:
        # 訂單留在 PENDING，caller 可用同一個 reference_id 重試
        raise upstream_error(e)

    row = store.attach_paypal_id(row["id"], pp_order["id"],
                                 pp_order.get("status", "CREATED"))
    return _out(row, approve=pp.approve_url(pp_order))


@router.post("/{order_id}/capture")
def capture(order_id: str, caller: Caller = Depends(require("orders:write"))):
    row = store.get(caller.caller_id, order_id)
    if not row:
        raise not_found("order")          # 別人的訂單也走這裡，不是 403
    if not row.get("paypal_order_id"):
        raise bad_request("這筆訂單還沒有 PayPal order id，無法 capture")
    try:
        res = pp.capture_order(row["paypal_order_id"])
    except PayPalError as e:
        raise upstream_error(e)
    row = store.set_status(row["id"], res.get("status", "COMPLETED"),
                           captured=True)
    return _out(row)


@router.post("/{order_id}/refund")
def refund(order_id: str, body: RefundCreate,
           caller: Caller = Depends(require("orders:write"))):
    row = store.get(caller.caller_id, order_id)
    if not row:
        raise not_found("order")
    try:
        pp_order = pp.get_order(row["paypal_order_id"])
        capture_id = pp.capture_id_of(pp_order)
        if not capture_id:
            raise bad_request("這筆訂單還沒有 capture，無法退款")
        amount = None
        if body.amount is not None:
            amount = validate_amount(body.amount, row["currency"])
        res = pp.refund_capture(capture_id, amount=amount,
                                currency=row["currency"], note=body.note)
    except (UnsupportedCurrency, InvalidAmount) as e:
        raise bad_request(e)
    except PayPalError as e:
        raise upstream_error(e)
    status = "REFUNDED" if body.amount is None else "PARTIALLY_REFUNDED"
    row = store.set_status(row["id"], status)
    return {**_out(row), "refund_id": res.get("id")}


@router.get("")
def list_orders(status: Optional[str] = None,
                limit: int = Query(default=50, ge=1, le=200),
                offset: int = Query(default=0, ge=0),
                caller: Caller = Depends(require("orders:read"))):
    rows = store.list_(caller.caller_id, status=status, limit=limit, offset=offset)
    return {"items": [_out(r) for r in rows]}


@router.get("/{order_id}")
def get_order(order_id: str, caller: Caller = Depends(require("orders:read"))):
    row = store.get(caller.caller_id, order_id)
    if not row:
        raise not_found("order")
    return _out(row)

