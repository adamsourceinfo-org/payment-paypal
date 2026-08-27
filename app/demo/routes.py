"""付款模擬頁的 HTTP 形狀。**只在 sandbox 掛得上**（見 app/main.py）。

它是給人操作的驗證工具，**不是 caller 的接入範例** —— 它就是服務本人，
沒有 API key、沒有跨服務的信任邊界。caller 要抄的東西在 README 的
〈怎麼接事件推送〉。
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.demo import flows
from app.urls import base_url

log = logging.getLogger("demo")

router = APIRouter(prefix="/demo", tags=["demo"])

_PAGE = Path(__file__).with_name("page.html")


@router.get("", response_class=HTMLResponse)
def page() -> HTMLResponse:
    """單一靜態檔。刻意不引 Jinja2 —— 這個 repo 連 DB driver 都挑不用編譯的，
    為了一組 dev 用的頁面拉進樣板引擎不划算。動態部分由瀏覽器打
    /demo/api/* 拿 JSON。"""
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


class _AmountIn(BaseModel):
    amount: str = Field(default="9.99")


@router.post("/api/orders")
def api_create_order(body: _AmountIn, request: Request):
    return flows.start_order(body.amount, base_url(request))


@router.get("/return/order/{reference_id}")
def order_return(reference_id: str):
    """PayPal 把**使用者的瀏覽器**導回這裡。導回一律回 303 到 /demo，
    讓網址列乾淨、重新整理也不會再 capture 一次。"""
    result = flows.finish_order(reference_id)
    return RedirectResponse(f"/demo?ref={reference_id}&result={result}", 303)


@router.get("/cancel/order/{reference_id}")
def order_cancel(reference_id: str):
    return RedirectResponse(f"/demo?ref={reference_id}&result=cancelled", 303)


@router.post("/api/subscriptions")
def api_create_subscription(request: Request):
    return flows.start_subscription(base_url(request))


@router.get("/return/subscription/{reference_id}")
def subscription_return(reference_id: str):
    """⚠️ 這裡**不做任何事**，只導回。訂閱轉 ACTIVE 是 webhook 的工作 ——
    在這裡搶著去 PayPal 問一次只會在導回路徑上多一次外部呼叫，而導回要快。"""
    return RedirectResponse(f"/demo?ref={reference_id}&result=subscribed", 303)


@router.get("/cancel/subscription/{reference_id}")
def subscription_cancel(reference_id: str):
    return RedirectResponse(f"/demo?ref={reference_id}&result=cancelled", 303)
