from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.web import flash

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Тонкий неразрывный пробел: разряды видно, а перенос строки посреди
# числа невозможен.
GROUP_SEPARATOR = " "
DASH = "—"


def format_amount(value, max_decimals: int = 8) -> str:
    """Количество монет в человеческом виде.

    Decimal.normalize() сам по себе выдаёт 1.5E+8 — в таблице портфеля
    это читается как ошибка. Здесь всегда позиционная запись, без хвоста
    незначащих нулей и с разделением разрядов.
    """
    if value is None:
        return DASH
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return DASH

    quantum = Decimal(1).scaleb(-max_decimals)
    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -max_decimals:
        number = number.quantize(quantum)

    text = format(number.normalize(), "f")
    return _group(text)


def format_usd(value, decimals: int = 2) -> str:
    if value is None:
        return DASH
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return DASH

    text = _group(format(number.quantize(Decimal(1).scaleb(-decimals)), "f"))
    # Знак ставится перед символом валюты: «-$500», а не «$-500».
    if text.startswith("-"):
        return f"-${text[1:]}"
    return f"${text}"


def format_pct(value, decimals: int = 2, *, signed: bool = False) -> str:
    if value is None:
        return DASH
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return DASH

    text = format(number.quantize(Decimal(1).scaleb(-decimals)), "f")
    if signed and number >= 0:
        text = f"+{text}"
    return f"{text}%"


def _group(text: str) -> str:
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]

    whole, _, fraction = text.partition(".")
    grouped = f"{int(whole):,}".replace(",", GROUP_SEPARATOR)
    return sign + grouped + (f".{fraction}" if fraction else "")


def _flash_messages(request: Request) -> dict:
    """Отдать шаблону накопленные сообщения и очистить очередь.

    Подключено обработчиком контекста, чтобы каждый роутер не тащил их
    в контекст руками и не забывал об этом.
    """
    return {"flashes": flash.pop_flashes(request)}


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    context_processors=[_flash_messages],
)
templates.env.filters["amount"] = format_amount
templates.env.filters["usd"] = format_usd
templates.env.filters["pct"] = format_pct
