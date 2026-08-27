"""付款模擬頁的 HTTP 形狀。**只在 sandbox 掛得上**（見 app/main.py）。

它是給人操作的驗證工具，**不是 caller 的接入範例** —— 它就是服務本人，
沒有 API key、沒有跨服務的信任邊界。caller 要抄的東西在 README 的
〈怎麼接事件推送〉。
"""
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = logging.getLogger("demo")

router = APIRouter(prefix="/demo", tags=["demo"])

_PAGE = Path(__file__).with_name("page.html")


@router.get("", response_class=HTMLResponse)
def page() -> HTMLResponse:
    """單一靜態檔。刻意不引 Jinja2 —— 這個 repo 連 DB driver 都挑不用編譯的，
    為了一組 dev 用的頁面拉進樣板引擎不划算。動態部分由瀏覽器打
    /demo/api/* 拿 JSON。"""
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))
