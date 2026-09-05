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

    # У монет вроде BABYDOGE цена меньше 1e-8, и жёсткие восемь знаков
    # после запятой превращали бы её в ноль. Для значений меньше единицы
    # считаем не знаки после запятой, а значащие цифры.
    if number != 0 and abs(number) < 1:
        leading_zeros = -number.adjusted() - 1
        max_decimals = min(18, max(max_decimals, leading_zeros + 4))

    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -max_decimals:
        number = number.quantize(Decimal(1).scaleb(-max_decimals))

    text = format(number.normalize(), "f")
    return _group(text)


def format_usd(value, decimals: int = 2, *, signed: bool = False) -> str:
    """Сумма в долларах. signed нужен там, где важен знак прибыли.

    Для PnL плюс не менее содержателен, чем минус: «$120» и «+$120»
    читаются по-разному, когда рядом в колонке стоят убытки.
    """
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
    return f"+${text}" if signed else f"${text}"


def format_usd_short(value) -> str:
    """Крупная сумма коротко: $2.71 трлн вместо тринадцати цифр подряд.

    Капитализация рынка в полном виде нечитаема и ломает вёрстку карточки.
    """
    if value is None:
        return DASH
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return DASH

    for limit, suffix in (
        (Decimal("1e12"), "трлн"),
        (Decimal("1e9"), "млрд"),
        (Decimal("1e6"), "млн"),
    ):
        if abs(number) >= limit:
            scaled = (number / limit).quantize(Decimal("0.01"))
            return f"${scaled} {suffix}"

    return format_usd(number)


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


def static_version() -> str:
    """Метка версии статики для обхода кэша браузера.

    Без неё после деплоя пользователь продолжает видеть старый CSS, пока
    не сбросит кэш вручную. Берём время изменения таблицы стилей:
    меняется при каждой сборке образа и не требует отдельного шага.
    """
    stylesheet = STATIC_DIR / "css" / "style.css"
    try:
        return str(int(stylesheet.stat().st_mtime))
    except OSError:
        return "0"


STATIC_VERSION = static_version()


def _flash_messages(request: Request) -> dict:
    """Отдать шаблону накопленные сообщения и очистить очередь.

    Подключено обработчиком контекста, чтобы каждый роутер не тащил их
    в контекст руками и не забывал об этом.
    """
    return {"flashes": flash.pop_flashes(request), "static_version": STATIC_VERSION}


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    context_processors=[_flash_messages],
)
templates.env.filters["amount"] = format_amount
templates.env.filters["usd"] = format_usd
templates.env.filters["usd_short"] = format_usd_short
templates.env.filters["pct"] = format_pct
