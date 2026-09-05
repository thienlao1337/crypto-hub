"""Разбор команд бота.

Пользователь пишет команду руками, поэтому разбор должен прощать
мелочи — лишние пробелы, запятую вместо точки, нижний регистр — и при
этом внятно отказывать на бессмыслице.
"""

from decimal import Decimal

import pytest

from app.bot import formatting
from app.services import alert_service


@pytest.mark.parametrize(
    ("text", "symbol", "type_code", "level"),
    [
        ("BTC > 70000", "BTC", alert_service.TYPE_PRICE_ABOVE, Decimal("70000")),
        ("btc>70000", "BTC", alert_service.TYPE_PRICE_ABOVE, Decimal("70000")),
        ("  ETH  <  2500  ", "ETH", alert_service.TYPE_PRICE_BELOW, Decimal("2500")),
        ("SOL >= 150.5", "SOL", alert_service.TYPE_PRICE_ABOVE, Decimal("150.5")),
        ("PEPE <= 0.000001", "PEPE", alert_service.TYPE_PRICE_BELOW, Decimal("0.000001")),
        # Запятая как десятичный разделитель — привычка русской раскладки.
        ("BTC > 70000,5", "BTC", alert_service.TYPE_PRICE_ABOVE, Decimal("70000.5")),
        # Пробелы внутри числа: так копируют из интерфейса.
        ("BTC > 70 000", "BTC", alert_service.TYPE_PRICE_ABOVE, Decimal("70000")),
    ],
)
def test_parse_alert(text, symbol, type_code, level):
    request = formatting.parse_alert(text)

    assert request.symbol == symbol
    assert request.type_code == type_code
    assert request.level == level


@pytest.mark.parametrize(
    "text",
    ["", "BTC", "BTC 70000", "> 70000", "BTC > ", "BTC > абв", "BTC > 0", "BTC > -5"],
)
def test_parse_alert_rejects_nonsense(text):
    with pytest.raises(formatting.CommandError):
        formatting.parse_alert(text)


def test_parse_alert_error_shows_example():
    """Отказ должен подсказывать формат, а не просто ругаться."""
    with pytest.raises(formatting.CommandError) as exc:
        formatting.parse_alert("непонятно что")

    assert "/alert" in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("btc", "BTC/USDT"),
        ("BTC", "BTC/USDT"),
        (" eth ", "ETH/USDT"),
        ("eth/usdc", "ETH/USDC"),
        ("SOL-USDT", "SOL/USDT"),
    ],
)
def test_normalize_symbol(raw, expected):
    assert formatting.normalize_symbol(raw) == expected


def test_normalize_symbol_requires_input():
    with pytest.raises(formatting.CommandError):
        formatting.normalize_symbol("   ")


def test_number_has_no_zero_tail():
    assert formatting.number(Decimal("79761.900000000000000000")) == "79761.9"
    assert formatting.number(Decimal("150000000")) == "150000000"
    assert formatting.number(None) == "—"


def test_money_and_percent():
    assert formatting.money(Decimal("85054.5")) == "$85 054.50"
    assert formatting.percent(Decimal("5.256")) == "+5.26%"
    assert formatting.percent(Decimal("-1.6")) == "-1.60%"
    assert formatting.percent(None) == "—"


def test_arrow_marks_direction():
    """В Telegram нет цвета, направление показывает стрелка."""
    assert formatting.arrow(Decimal("1")) == "▲"
    assert formatting.arrow(Decimal("-1")) == "▼"
    assert formatting.arrow(None) == "•"
