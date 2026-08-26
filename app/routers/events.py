from fastapi import APIRouter, Depends, Query

from app.auth import Caller, require
from app.event_view import item
from app.store import events as store

router = APIRouter(prefix="/v1/events", tags=["events"])

MAX_LIMIT = 500


@router.get("")
def list_events(after: int = Query(default=0, ge=0),
                limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
                caller: Caller = Depends(require("events:read"))):
    """游標式增量拉取。caller 記住最後一筆的 id 當下次的 after。

    傳 after=0 可以從頭拉，這也是對帳的路徑。

    **這條路永遠保留，而且它是推送的安全網。** 推送（見
    `PUT /v1/webhook-endpoint`）是**第二條**出口，不是取代 —— 沒註冊端點的
    caller 完全不受影響，游標語意一個位元組都沒變。

    ⚠️ `items[]` 的形狀由 `app/event_view.py` 的 `item()` 定義**一次**，
    推送的 body 用的是同一個函式。不要在這裡手抄一份 dict ——
    兩個形狀就是兩份程式碼、兩組 bug，而其中一份平常不會執行。
    """
    rows = store.list_after(caller.caller_id, after, limit)
    return {
        "items": [item(r) for r in rows],
        "next_cursor": rows[-1]["id"] if rows else after,
    }
