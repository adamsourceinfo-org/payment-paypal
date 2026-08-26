import pytest
from app.config import load_settings, PAYPAL_API_BASE

BASE = {
    "PAYPAL_ENV": "sandbox",
    "PAYPAL_CLIENT_ID": "cid",
    "PAYPAL_CLIENT_SECRET": "csecret",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("PAYPAL_ENV", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET",
              "PAYPAL_WEBHOOK_ID", "SUPPORTED_CURRENCIES",
              "WEBHOOK_SIGNING_KEY", "INTERNAL_KEY", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_loads_required(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.paypal_env == "sandbox"
    assert s.paypal_client_id == "cid"
    assert s.supported_currencies == frozenset({"USD"})
    assert s.paypal_webhook_id is None


def test_missing_required_raises(monkeypatch):
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")
    with pytest.raises(RuntimeError) as e:
        load_settings()
    assert "PAYPAL_CLIENT_ID" in str(e.value)


def test_bad_paypal_env_raises(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PAYPAL_ENV", "production")
    with pytest.raises(RuntimeError):
        load_settings()


def test_api_base_is_derived():
    assert PAYPAL_API_BASE["sandbox"] == "https://api-m.sandbox.paypal.com"
    assert PAYPAL_API_BASE["live"] == "https://api-m.paypal.com"


def test_paypal_api_base_property(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    assert load_settings().paypal_api_base == "https://api-m.sandbox.paypal.com"


def test_currencies_parsed(monkeypatch):
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SUPPORTED_CURRENCIES", "USD, eur")
    assert load_settings().supported_currencies == frozenset({"USD", "EUR"})


# --- 推送的設定 -------------------------------------------------------

def test_兩把機密缺席時不啟動失敗_只是關掉推送(monkeypatch):
    """第一次部署時 secret 還沒建，硬性必填會讓服務起不來 ——
    而沒有推送的服務仍然是完全可用的服務。"""
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.webhook_signing_key is None and s.internal_key is None
    assert s.push_configured is False


def test_只有一把也算沒設定(monkeypatch):
    """一把用來簽給 caller，一把用來認自己的內部端點 —— 缺一不可。"""
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WEBHOOK_SIGNING_KEY", "abc")
    assert load_settings().push_configured is False


def test_可選機密的前後空白要被strip掉(monkeypatch):
    """⚠️ Secret Manager 存的是位元組，而最自然的建立方式
    （`python3 -c 'print(...)' | gcloud secrets create --data-file=-`）
    會把**換行也存進去**，Cloud Run 原樣注入。

    症狀是「帶對金鑰仍然回 401」—— 比對的另一邊是 shell 展開時 trim 過的。
    這是 payment-ecpay 實跑 dev 才抓到的，本地永遠測不到，所以釘在這裡。
    """
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("INTERNAL_KEY", "abc123\n")
    monkeypatch.setenv("WEBHOOK_SIGNING_KEY", " def456 ")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-1\n")
    s = load_settings()
    assert s.internal_key == "abc123"
    assert s.webhook_signing_key == "def456"
    assert s.paypal_webhook_id == "WH-1"


def test_queue名稱與位置有預設值(monkeypatch):
    """兩個環境一模一樣，所以放 .cicd/env.common —— 抄兩遍的東西遲早會分歧。
    環境靠 project ID 識別（向 metadata server 要），queue 名字不必分環境。"""
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.tasks_queue_prefix == "payment-paypal-deliveries"
    assert s.tasks_location == "asia-east1"
