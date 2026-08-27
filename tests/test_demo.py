"""付款模擬頁。

⚠️ 這裡最重要的一條是「live 環境根本沒有這些路由」——
prod 是 live，一筆 demo 訂單就是一筆真的收款。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _probe_app(fake_settings, paypal_env: str) -> FastAPI:
    """用指定的 paypal_env 重新掛一次 router，回一個乾淨的 app。

    ⚠️ 不用 importlib.reload(app.main) —— 那會把模組狀態留給後面的測試。
    掛載決定抽成 mount_routers(app) 就是為了讓這件事測得起來。

    ⚠️ `app.main` 一定要在**函式裡面**才 import。它在模組層就會跑
    `mount_routers(app)`，而那會呼叫 get_settings() —— 在測試模組頂端 import
    的話，那一行發生在 autouse 的 fake_settings fixture 之前，
    會去讀真的環境變數然後死在「缺少必要環境變數 PAYPAL_ENV」。
    """
    import app.main as main

    fake_settings.paypal_env = paypal_env
    probe = FastAPI()
    main.mount_routers(probe)
    return probe


def test_live環境沒有demo路由(fake_settings):
    """prod 是 live。這條測試就是 prod 隔離的全部實作。"""
    probe = _probe_app(fake_settings, "live")
    assert "/demo" not in {r.path for r in probe.routes}
    assert TestClient(probe).get("/demo").status_code == 404


def test_sandbox環境掛得出頁面(fake_settings):
    probe = _probe_app(fake_settings, "sandbox")
    r = TestClient(probe).get("/demo")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_demo身分固定是demo(fake_settings):
    from app.demo.flows import DEMO_CALLER, DEMO_CALLER_ID

    assert DEMO_CALLER_ID == "demo"
    assert DEMO_CALLER.caller_id == "demo"
