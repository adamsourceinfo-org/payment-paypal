import pytest
from decimal import Decimal

from app.money import validate_amount, format_amount, DECIMALS
from app.errors import UnsupportedCurrency, InvalidAmount


@pytest.fixture(autouse=True)
def usd_only(monkeypatch, fake_settings):
    monkeypatch.setattr(fake_settings, "supported_currencies", frozenset({"USD"}))


# ── 正規化：一律變成該幣別的位數 ──────────────────────────────────────
def test_integer_gets_two_decimals():
    """caller 傳整數 → 補成 .00"""
    assert validate_amount("10", "USD") == Decimal("10.00")
    assert format_amount(validate_amount("10", "USD"), "USD") == "10.00"


def test_one_decimal_padded():
    assert format_amount(validate_amount("10.5", "USD"), "USD") == "10.50"


def test_two_decimals_unchanged():
    assert format_amount(validate_amount("10.99", "USD"), "USD") == "10.99"


def test_db_style_four_decimals_render_as_two():
    """DB 是 numeric(18,4)，讀回來是 25.0000，對外要是 25.00"""
    assert format_amount(Decimal("25.0000"), "USD") == "25.00"


# ── 超過位數：回錯誤且指名欄位 ────────────────────────────────────────
def test_three_decimals_rejected_and_names_field():
    with pytest.raises(InvalidAmount) as e:
        validate_amount("10.001", "USD")
    assert e.value.field == "amount"
    assert "10.001" in str(e.value)


def test_unsupported_currency_names_field():
    with pytest.raises(UnsupportedCurrency) as e:
        validate_amount("10.00", "EUR")
    assert e.value.field == "currency"
    assert "USD" in str(e.value)


def test_zero_and_negative_rejected():
    for bad in ("0", "0.00", "-1.00"):
        with pytest.raises(InvalidAmount) as e:
            validate_amount(bad, "USD")
        assert e.value.field == "amount"


def test_garbage_rejected():
    with pytest.raises(InvalidAmount) as e:
        validate_amount("abc", "USD")
    assert e.value.field == "amount"


def test_lowercase_currency_accepted():
    assert validate_amount("10.00", "usd") == Decimal("10.00")


def test_no_decimal_currency_rule_intact():
    """TWD/JPY/HUF 在 PayPal 不接受小數 —— 位數是幣別屬性，不是寫死 2"""
    assert DECIMALS["TWD"] == 0 and DECIMALS["JPY"] == 0 and DECIMALS["USD"] == 2
