"""Разбор команд и оформление ответов бота.

Вынесено отдельно от обработчиков: разбор строки вроде «BTC > 70000» —
чистая функция, и проверять её удобнее без Telegram.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.services import alert_service

# «/alert BTC > 70000», «BTC>70000», «btc < 60 000.5»
ALERT_PATTERN = re.compile(
    r"^\s*(?P<symbol>[A-Za-z0-9]{2,15})\s*(?P<op>>=|<=|>|<)\s*(?P<value>[\d\s.,]+)\s*$"
)


class CommandError(Exception):
    """Команду не удалось разобрать — сообщение уйдёт пользователю."""


@dataclass(frozen=True)
class AlertRequest:
    symbol: str
    type_code: str
    level: Decimal


def parse_alert(text: str) -> AlertRequest:
    """Разобрать «BTC > 70000» в параметры алерта."""
    match = ALERT_PATTERN.match(text or "")
    if match is None:
        raise CommandError(
            "Не разобрал условие. Формат: <code>/alert BTC > 70000</code>"
        )

    raw_value = match.group("value").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        level = Decimal(raw_value)
    except (InvalidOperation, ValueError) as exc:
        raise CommandError("Уровень цены должен быть числом.") from exc

    if level <= 0:
        raise CommandError("Уровень цены должен быть больше нуля.")

    above = match.group("op").startswith(">")
    return AlertRequest(
        symbol=match.group("symbol").upper(),
        type_code=alert_service.TYPE_PRICE_ABOVE if above else alert_service.TYPE_PRICE_BELOW,
        level=level,
    )


def normalize_symbol(raw: str, *, quote: str = "USDT") -> str:
    """«btc» → «BTC/USDT», «eth/usdc» → «ETH/USDC»."""
    text = (raw or "").strip().upper().replace("-", "/")
    if not text:
        raise CommandError("Укажите монету, например <code>/price BTC</code>")
    if "/" in text:
        return text
    return f"{text}/{quote}"


def number(value) -> str:
    """Число без хвоста нулей и без экспоненты."""
    if value is None:
        return "—"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value.normalize(), "f")


def money(value) -> str:
    if value is None:
        return "—"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    text = f"{value.quantize(Decimal('0.01')):,}".replace(",", " ")
    # Знак перед символом валюты: «-$500», а не «$-500». Отрицательные
    # суммы здесь появляются у нереализованного PnL.
    if text.startswith("-"):
        return f"-${text[1:]}"
    return f"${text}"


def percent(value, *, signed: bool = True) -> str:
    if value is None:
        return "—"
    number_value = float(value)
    sign = "+" if signed and number_value >= 0 else ""
    return f"{sign}{number_value:.2f}%"


def arrow(value) -> str:
    """Стрелка вместо цвета: в Telegram разметки цветом нет."""
    if value is None:
        return "•"
    return "▲" if float(value) >= 0 else "▼"
