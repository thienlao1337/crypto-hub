from decimal import Decimal

import pytest

from app.web.templates_env import (
    GROUP_SEPARATOR as NB,  # узкий неразрывный пробел между разрядами
)
from app.web.templates_env import format_amount, format_pct, format_usd


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Ради этого случая фильтр и появился: Decimal.normalize() отдавал
        # 1.5E+8, и в таблице портфеля это выглядело как ошибка.
        (Decimal("150000000"), f"150{NB}000{NB}000"),
        (Decimal("180.000000000000000000"), "180"),
        (Decimal("16800.00"), f"16{NB}800"),
        (Decimal("0.42"), "0.42"),
        (Decimal("0.000001"), "0.000001"),
        (Decimal("6.5"), "6.5"),
        (Decimal("-12.5"), "-12.5"),
        (Decimal("0"), "0"),
        (None, "—"),
    ],
)
def test_format_amount(value, expected):
    assert format_amount(value) == expected


def test_amount_trims_excess_precision():
    """Восемнадцать знаков из базы в интерфейсе не нужны."""
    assert format_amount(Decimal("0.123456789012345678")) == "0.12345679"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("85054.59"), f"$85{NB}054.59"),
        (Decimal("1234.5"), f"$1{NB}234.50"),
        (Decimal("0"), "$0.00"),
        (Decimal("-500"), "-$500.00"),
        (None, "—"),
    ],
)
def test_format_usd(value, expected):
    assert format_usd(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("120.5"), "+$120.50"),
        (Decimal("-120.5"), "-$120.50"),
        (Decimal("0"), "+$0.00"),
    ],
)
def test_format_usd_signed(value, expected):
    """У PnL плюс так же содержателен, как минус."""
    assert format_usd(value, signed=True) == expected


@pytest.mark.parametrize(
    ("value", "signed", "expected"),
    [
        (Decimal("5.256"), True, "+5.26%"),
        (Decimal("-1.64"), True, "-1.64%"),
        (Decimal("39.3"), False, "39.30%"),
        (None, True, "—"),
    ],
)
def test_format_pct(value, signed, expected):
    assert format_pct(value, signed=signed) == expected


def test_pct_respects_decimals():
    assert format_pct(Decimal("39.34"), 1) == "39.3%"
