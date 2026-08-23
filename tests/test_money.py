import pytest
from decimal import Decimal

from app.money import validate_amount, format_amount
from app.errors import UnsupportedCurrency, InvalidAmount


@pytest.fixture(autouse=True)
def usd_only(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: type("S", (), {
        "supported_currencies": frozenset({"USD"})})())
    import app.money as m
    monkeypatch.setattr(m, "get_settings", cfg.get_settings)


def test_usd_two_decimals_ok():
    assert validate_amount("10.00", "USD") == Decimal("10.00")


def test_usd_integer_ok():
    assert validate_amount("10", "USD") == Decimal("10")


def test_usd_three_decimals_rejected():
    with pytest.raises(InvalidAmount):
        validate_amount("10.000", "USD")


def test_twd_not_supported_by_account():
    with pytest.raises(UnsupportedCurrency):
        validate_amount("300", "TWD")


def test_unsupported_currency_lists_supported():
    with pytest.raises(UnsupportedCurrency) as e:
        validate_amount("10.00", "EUR")
    assert "USD" in str(e.value)


def test_zero_and_negative_rejected():
    for bad in ("0.00", "-1.00"):
        with pytest.raises(InvalidAmount):
            validate_amount(bad, "USD")


def test_garbage_rejected():
    with pytest.raises(InvalidAmount):
        validate_amount("abc", "USD")


def test_lowercase_currency_accepted():
    assert validate_amount("10.00", "usd") == Decimal("10.00")


def test_format_amount_pads_to_currency_decimals():
    assert format_amount(Decimal("10"), "USD") == "10.00"
    assert format_amount(Decimal("300"), "TWD") == "300"


def test_format_amount_used_for_api_output():
    """API 回傳的金額要符合幣別位數 —— DB 是 numeric(18,4)，
    直接 str() 會變成 "25.0000"，對金流 API 來說是雜訊，caller 顯示時還得自己處理。"""
    from decimal import Decimal
    assert format_amount(Decimal("25.0000"), "USD") == "25.00"
    assert format_amount(Decimal("9.9900"), "USD") == "9.99"
