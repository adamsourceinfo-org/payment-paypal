"""幣別與金額驗證。在進門就擋，不要送到 PayPal 才被拒 ——
那時錯誤訊息對 caller 沒有幫助，而且浪費一次外部呼叫。"""
from decimal import Decimal, InvalidOperation

from app.config import get_settings
from app.errors import InvalidAmount, UnsupportedCurrency

# 小數位數是幣別的屬性，不是寫死的 2。
# PayPal 對 TWD / JPY / HUF 不接受小數，送 "300.00" 會被拒。
DECIMALS = {"USD": 2, "EUR": 2, "GBP": 2, "TWD": 0, "JPY": 0, "HUF": 0}


def validate_amount(amount: str, currency: str) -> Decimal:
    cur = (currency or "").strip().upper()
    supported = get_settings().supported_currencies
    if cur not in supported:
        raise UnsupportedCurrency(
            f"不支援的幣別 {cur!r}，目前支援：{sorted(supported)}")
    if cur not in DECIMALS:
        raise UnsupportedCurrency(f"幣別 {cur!r} 沒有定義小數位數規則")

    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        raise InvalidAmount(f"金額格式不正確：{amount!r}")

    if value <= 0:
        raise InvalidAmount("金額必須大於 0")

    exponent = -value.as_tuple().exponent
    allowed = DECIMALS[cur]
    if exponent > allowed:
        raise InvalidAmount(
            f"{cur} 最多 {allowed} 位小數，收到 {amount!r}")
    return value


def format_amount(value: Decimal, currency: str) -> str:
    """PayPal 的 value 欄位要字串，且位數要符合幣別。"""
    return f"{value:.{DECIMALS[currency.upper()]}f}"
