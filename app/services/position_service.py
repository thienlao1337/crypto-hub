"""Open positions and unrealized PnL.

A spot exchange doesn't return "positions": it returns a balance and a trade history.
The average entry price has to be assembled ourselves - by walking the trades from
oldest to newest with cost averaging. The same pass also computes the realized PnL of
each sale: without it the history shows what was sold and at what price, but not whether
it made or lost money.

How honest the number is depends on how complete the history is. Exchanges return a
limited period, so selling a coin bought before that period looks like a sale out of
nowhere, and a long-held position looks smaller than the balance. Neither case is hushed
up: the position is marked cost_basis_complete = False, and the UI shows the number with
a caveat instead of passing off an incomplete calculation as exact.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Asset,
    Balance,
    Exchange,
    ExchangeAccount,
    Market,
    MarketTicker,
    Position,
    Trade,
    User,
)
from app.models.portfolio import SIDE_BUY
from app.services import market_service

logger = logging.getLogger(__name__)

# How far a position built from trades may fall short of the balance before
# it's considered a sign of incomplete history. A small gap is always needed:
# fees are charged in the coin, and transfers between exchange wallets don't
# appear in the trade history.
BALANCE_TOLERANCE = Decimal("0.99")


@dataclass
class WalkResult:
    """Result of a pass over the trades of one pair."""

    amount: Decimal
    cost: Decimal
    opened_at: datetime | None
    complete: bool
    # One value per trade, in the same order: realized PnL of a sale or None
    # for a purchase.
    realized: list[Decimal | None] = field(default_factory=list)

    @property
    def entry_price(self) -> Decimal:
        return self.cost / self.amount if self.amount > 0 else Decimal(0)


@dataclass
class PositionView:
    """A row of the open positions table."""

    symbol: str
    exchange: str
    amount: Decimal
    entry_price: Decimal
    mark_price: Decimal | None
    cost_usd: Decimal | None
    value_usd: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pct: Decimal | None
    cost_basis_complete: bool


def walk_trades(
    trades: list[Trade], *, base_asset_id: int, quote_asset_id: int
) -> WalkResult:
    """Walk the trades of one pair and build the open position.

    The function is pure: it neither writes to nor reads from the database, so it can be
    checked on made-up trades without an exchange or a session.

    The fee is accounted for only if it was charged in the quote currency (the entry
    cost grows) or in the coin itself (the received amount shrinks). A fee in a third
    coin - discounted BNB and the like - has nothing to convert by and is ignored:
    understating the entry price by silently plugging in a rate is worse than not
    accounting for it.
    """
    amount = Decimal(0)
    cost = Decimal(0)
    opened_at: datetime | None = None
    complete = True
    realized: list[Decimal | None] = []

    for trade in trades:
        qty = trade.amount or Decimal(0)
        gross = trade.cost if trade.cost is not None else qty * trade.price
        fee = trade.fee or Decimal(0)
        fee_quote = fee if trade.fee_asset_id == quote_asset_id else Decimal(0)
        fee_base = fee if trade.fee_asset_id == base_asset_id else Decimal(0)

        if trade.side == SIDE_BUY:
            received = qty - fee_base
            if received <= 0:
                realized.append(None)
                continue
            if amount <= 0:
                opened_at = trade.executed_at
            amount += received
            cost += gross + fee_quote
            realized.append(None)
            continue

        if amount <= 0 or qty <= 0:
            # Sold something that isn't in the history: the purchase is outside
            # the period the exchange returns.
            complete = False
            realized.append(None)
            continue

        entry = cost / amount
        sold = min(qty, amount)
        if sold < qty:
            complete = False

        # Proceeds are taken in proportion to the closed part: if more was sold
        # than we know about, the remaining tail isn't ours.
        proceeds = (gross - fee_quote) * (sold / qty)
        realized.append(proceeds - entry * sold)

        amount -= sold
        cost -= entry * sold
        if amount <= 0:
            amount = Decimal(0)
            cost = Decimal(0)
            opened_at = None

    return WalkResult(
        amount=amount,
        cost=cost,
        opened_at=opened_at,
        complete=complete,
        realized=realized,
    )


async def rebuild_positions(session: AsyncSession, account: ExchangeAccount) -> int:
    """Rebuild the positions of one connection from the trade history.

    The recalculation is done in full, not incrementally: a catch-up sync may bring in a
    backdated trade, and then the average entry price changes for the whole chain after
    it.
    """
    markets = (
        await session.execute(
            select(Market.id, Market.base_asset_id, Market.quote_asset_id)
            .join(Trade, Trade.market_id == Market.id)
            .where(Trade.exchange_account_id == account.id)
            .distinct()
        )
    ).all()

    open_amount_by_asset: dict[int, Decimal] = {}
    open_markets: dict[int, int] = {}
    kept: set[int] = set()

    for market_id, base_asset_id, quote_asset_id in markets:
        trades = list(
            (
                await session.execute(
                    select(Trade)
                    .where(
                        Trade.exchange_account_id == account.id,
                        Trade.market_id == market_id,
                    )
                    .order_by(Trade.executed_at, Trade.id)
                )
            ).scalars()
        )

        result = walk_trades(
            trades, base_asset_id=base_asset_id, quote_asset_id=quote_asset_id
        )
        for trade, pnl in zip(trades, result.realized):
            trade.realized_pnl = pnl

        position = await _get_position(session, account.id, market_id)

        if result.amount <= 0:
            # The position is closed. We don't delete the existing row - it
            # shows that the pair was traded and is currently empty - but we
            # don't create a new one for a zero balance either: a list of
            # closed positions would duplicate the trade history.
            if position is not None:
                position.amount = Decimal(0)
                position.is_open = False
                position.mark_price = None
                position.unrealized_pnl = None
            continue

        if position is None:
            position = Position(
                exchange_account_id=account.id,
                market_id=market_id,
                side=SIDE_BUY,
            )
            session.add(position)

        position.amount = result.amount
        position.entry_price = result.entry_price
        position.opened_at = result.opened_at
        position.is_open = True
        position.cost_basis_complete = result.complete

        kept.add(market_id)
        open_markets[market_id] = base_asset_id
        open_amount_by_asset[base_asset_id] = (
            open_amount_by_asset.get(base_asset_id, Decimal(0)) + result.amount
        )

    await _flag_against_balances(session, account, open_amount_by_asset, open_markets)
    await session.flush()
    return len(kept)


async def mark_positions(session: AsyncSession) -> int:
    """Set the current price and unrealized PnL on open positions.

    Called right after the quote refresh so that the valuation and the price snapshot
    it's based on come from the same moment.
    """
    prices = await market_service.build_usd_price_map(session)

    rows = (
        await session.execute(
            select(Position, MarketTicker.last, Asset.symbol)
            .join(Market, Market.id == Position.market_id)
            .join(Asset, Asset.id == Market.quote_asset_id)
            .outerjoin(MarketTicker, MarketTicker.market_id == Position.market_id)
            .where(Position.is_open.is_(True))
        )
    ).all()

    updated = 0
    for position, last, quote_symbol in rows:
        if last is None or last <= 0:
            continue

        position.mark_price = last
        rate = prices.get(quote_symbol)
        if rate is None:
            # Pair against a quote we can't value: we can show the price, but
            # not the dollars.
            position.unrealized_pnl = None
            continue

        position.unrealized_pnl = (last - position.entry_price) * position.amount * rate
        updated += 1

    await session.flush()
    return updated


async def list_positions(session: AsyncSession, user: User) -> list[PositionView]:
    """The user's open positions, largest first."""
    account_ids = [
        account_id
        for (account_id,) in await session.execute(
            select(ExchangeAccount.id).where(ExchangeAccount.user_id == user.id)
        )
    ]
    if not account_ids:
        return []

    prices = await market_service.build_usd_price_map(session)

    rows = (
        await session.execute(
            select(Position, Market.symbol, Exchange.code, Asset.symbol)
            .join(Market, Market.id == Position.market_id)
            .join(Exchange, Exchange.id == Market.exchange_id)
            .join(Asset, Asset.id == Market.quote_asset_id)
            .where(
                Position.exchange_account_id.in_(account_ids),
                Position.is_open.is_(True),
                Position.amount > 0,
            )
        )
    ).all()

    views: list[PositionView] = []
    for position, symbol, exchange_code, quote_symbol in rows:
        rate = prices.get(quote_symbol)
        cost_usd = position.entry_price * position.amount * rate if rate else None

        value_usd = None
        pct = None
        if position.mark_price is not None and rate is not None:
            value_usd = position.mark_price * position.amount * rate
        if position.mark_price is not None and position.entry_price > 0:
            pct = (
                (position.mark_price - position.entry_price)
                / position.entry_price
                * Decimal(100)
            )

        views.append(
            PositionView(
                symbol=symbol,
                exchange=exchange_code,
                amount=position.amount,
                entry_price=position.entry_price,
                mark_price=position.mark_price,
                cost_usd=cost_usd,
                value_usd=value_usd,
                unrealized_pnl=position.unrealized_pnl,
                unrealized_pct=pct,
                cost_basis_complete=position.cost_basis_complete,
            )
        )

    views.sort(key=lambda v: -(v.value_usd or v.cost_usd or Decimal(0)))
    return views


def total_unrealized(views: list[PositionView]) -> Decimal | None:
    """Total unrealized PnL over valued positions.

    None means "nothing to compute from", not zero: an empty portfolio and a portfolio
    without quotes are different states.
    """
    known = [v.unrealized_pnl for v in views if v.unrealized_pnl is not None]
    return sum(known, Decimal(0)) if known else None


# --- Helpers ---


async def _get_position(
    session: AsyncSession, account_id: int, market_id: int
) -> Position | None:
    result = await session.execute(
        select(Position).where(
            Position.exchange_account_id == account_id,
            Position.market_id == market_id,
            Position.side == SIDE_BUY,
        )
    )
    return result.scalar_one_or_none()


async def _flag_against_balances(
    session: AsyncSession,
    account: ExchangeAccount,
    open_amount_by_asset: dict[int, Decimal],
    open_markets: dict[int, int],
) -> None:
    """Reconcile positions with the balance and flag incomplete ones.

    A position built from trades should match what sits on the exchange. If the balance
    holds noticeably more of the coin than the history explains, some purchases are
    outside the returned period - the average entry price doesn't describe the whole
    balance, and that needs to be said plainly.
    """
    if not open_amount_by_asset:
        return

    balances = {
        asset_id: total
        for asset_id, total in await session.execute(
            select(Balance.asset_id, Balance.total).where(
                Balance.exchange_account_id == account.id,
                Balance.asset_id.in_(open_amount_by_asset),
            )
        )
    }

    incomplete_assets = {
        asset_id
        for asset_id, derived in open_amount_by_asset.items()
        if (balance := balances.get(asset_id)) is not None
        and derived < balance * BALANCE_TOLERANCE
    }
    if not incomplete_assets:
        return

    for market_id, base_asset_id in open_markets.items():
        if base_asset_id not in incomplete_assets:
            continue
        position = await _get_position(session, account.id, market_id)
        if position is not None:
            position.cost_basis_complete = False
