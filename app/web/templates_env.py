from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.requests import Request

from app.services.localtime import in_zone
from app.web import flash

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Thin non-breaking space: digit groups are visible, and a line break in the
# middle of a number is impossible.
GROUP_SEPARATOR = " "
DASH = "—"


def format_amount(value, max_decimals: int = 8) -> str:
    """A coin amount in human-readable form.

    Decimal.normalize() on its own produces 1.5E+8 - in the portfolio table that reads
    as an error. Here it's always positional notation, without trailing insignificant
    zeros and with digit grouping.
    """
    if value is None:
        return DASH
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return DASH

    # Coins like BABYDOGE have prices below 1e-8, and a fixed eight decimal
    # places would turn them into zero. For values below one we count
    # significant digits instead of decimal places.
    if number != 0 and abs(number) < 1:
        leading_zeros = -number.adjusted() - 1
        max_decimals = min(18, max(max_decimals, leading_zeros + 4))

    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -max_decimals:
        number = number.quantize(Decimal(1).scaleb(-max_decimals))

    text = format(number.normalize(), "f")
    return _group(text)


def format_usd(value, decimals: int = 2, *, signed: bool = False) -> str:
    """An amount in dollars. signed is used where the sign of profit matters.

    For PnL a plus is as informative as a minus: "$120" and "+$120" read differently
    when there are losses next to them in the column.
    """
    if value is None:
        return DASH
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return DASH

    text = _group(format(number.quantize(Decimal(1).scaleb(-decimals)), "f"))
    # The sign goes before the currency symbol: "-$500", not "$-500".
    if text.startswith("-"):
        return f"-${text[1:]}"
    return f"+${text}" if signed else f"${text}"


def format_plain(value) -> str:
    """A number for an input field: no grouping, no percent sign, no trailing zeros.

    Deliberately separate from the display filters. With "-1.00%" or "1 000" with a
    non-breaking space in the form's value, the form can't be submitted: the server
    won't parse it, and the rule would stop saving after a single extra click on "Save".
    """
    if value is None:
        return ""
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return format(number.normalize(), "f")


def format_usd_short(value) -> str:
    """A large amount, short: $2.71 trillion instead of thirteen digits in a row.

    The market cap in full is unreadable and breaks the card layout.
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


@pass_context
def format_moment(context, value, fmt: str = "%d.%m %H:%M") -> str:
    """Time in the user's time zone.

    The filter takes the zone from current_user right in the template context: otherwise
    every router would have to pass it into the context by hand and would forget it one
    day, and the page would silently show UTC - the nastiest kind of bug, because it
    looks plausible.
    """
    if value is None:
        return DASH
    user = context.get("current_user")
    return in_zone(value, getattr(user, "timezone", None)).strftime(fmt)


def _group(text: str) -> str:
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]

    whole, _, fraction = text.partition(".")
    grouped = f"{int(whole):,}".replace(",", GROUP_SEPARATOR)
    return sign + grouped + (f".{fraction}" if fraction else "")


def static_version() -> str:
    """Static asset version tag for busting the browser cache.

    Without it, after a deploy the user keeps seeing the old CSS until they clear the
    cache by hand. We take the stylesheet's modification time: it changes with every
    image build and needs no extra step.
    """
    stylesheet = STATIC_DIR / "css" / "style.css"
    try:
        return str(int(stylesheet.stat().st_mtime))
    except OSError:
        return "0"


STATIC_VERSION = static_version()


def _flash_messages(request: Request) -> dict:
    """Pass accumulated messages to the template and clear the queue.

    Hooked up as a context processor so that every router doesn't have to pass them into
    the context by hand and forget to.
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
templates.env.filters["moment"] = format_moment
templates.env.filters["plain"] = format_plain
