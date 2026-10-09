"""Converter and trade calculator.

Calculations are pure functions over Decimal: checked by tests, not by eye. Rounding is
deliberately left to the output: intermediate values are computed at full precision,
otherwise rounding "eats" the fee.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import market_service


class ToolsError(Exception):
    """Invalid input - the message goes to the user."""


@dataclass(frozen=True)
class Conversion:
    amount: Decimal
    source: str
    target: str
    result: Decimal
    source_price_usd: Decimal
    target_price_usd: Decimal


@dataclass(frozen=True)
class TradeResult:
    side: str
    amount: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_cost: Decimal
    exit_proceeds: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    total_fees: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    net_pnl_pct: Decimal
    breakeven_price: Decimal


async def convert(
    session: AsyncSession, *, amount: Decimal, source: str, target: str
) -> Conversion:
    """Convert an amount of one coin into another via the dollar price."""
    source = source.strip().upper()
    target = target.strip().upper()
    if not source or not target:
        raise ToolsError("Укажите обе монеты.")
    if amount <= 0:
        raise ToolsError("Количество должно быть больше нуля.")

    prices = await market_service.build_usd_price_map(session)

    source_price = prices.get(source)
    target_price = prices.get(target)
    if source_price is None:
        raise ToolsError(f"Нет цены для {source}: пары к стейблкоину не найдено.")
    if target_price is None:
        raise ToolsError(f"Нет цены для {target}: пары к стейблкоину не найдено.")

    return Conversion(
        amount=amount,
        source=source,
        target=target,
        result=amount * source_price / target_price,
        source_price_usd=source_price,
        target_price_usd=target_price,
    )


def calculate_trade(
    *,
    side: str,
    amount: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    fee_pct: Decimal,
) -> TradeResult:
    """Trade result including the fee on entry and exit.

    The fee is charged twice - on the buy and on the sell. Counting it once, as
    calculators often do, overstates the profit.
    """
    if amount <= 0:
        raise ToolsError("Количество должно быть больше нуля.")
    if entry_price <= 0 or exit_price <= 0:
        raise ToolsError("Цены должны быть больше нуля.")
    if fee_pct < 0:
        raise ToolsError("Комиссия не может быть отрицательной.")

    side = side.lower()
    if side not in ("buy", "sell"):
        raise ToolsError("Направление сделки должно быть buy или sell.")

    fee_rate = fee_pct / Decimal(100)
    entry_cost = amount * entry_price
    exit_proceeds = amount * exit_price

    entry_fee = entry_cost * fee_rate
    exit_fee = exit_proceeds * fee_rate
    total_fees = entry_fee + exit_fee

    # For a short position, profit comes from a falling price.
    gross_pnl = (
        exit_proceeds - entry_cost if side == "buy" else entry_cost - exit_proceeds
    )
    net_pnl = gross_pnl - total_fees

    # The price at which the trade breaks even after both fees.
    if side == "buy":
        breakeven = entry_price * (1 + fee_rate) / (1 - fee_rate)
    else:
        breakeven = entry_price * (1 - fee_rate) / (1 + fee_rate)

    return TradeResult(
        side=side,
        amount=amount,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_cost=entry_cost,
        exit_proceeds=exit_proceeds,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        total_fees=total_fees,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        net_pnl_pct=net_pnl / entry_cost * Decimal(100) if entry_cost else Decimal(0),
        breakeven_price=breakeven,
    )


def parse_decimal(raw: str, field: str) -> Decimal:
    """Parse a number from a form, forgiving commas and spaces."""
    text = (raw or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        raise ToolsError(f"Заполните поле «{field}».")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ToolsError(f"Поле «{field}»: нужно число.") from exc
