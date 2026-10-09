"""Portfolio: balances, valuation, value history, trades."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.base import ExchangeAdapter
from app.models import (
    Asset,
    Balance,
    Exchange,
    ExchangeAccount,
    Market,
    MarketTicker,
    PortfolioSnapshot,
    Trade,
    User,
)
from app.services import market_service

logger = logging.getLogger(__name__)

PERIODS = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


@dataclass
class Holding:
    """Position in one coin, aggregated across all connected exchanges."""

    asset_symbol: str
    total: Decimal
    usd_value: Decimal | None
    change_24h_pct: Decimal | None = None
    share_pct: Decimal | None = None
    by_exchange: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class PortfolioSummary:
    total_usd: Decimal
    holdings: list[Holding]
    by_exchange: dict[str, Decimal]
    # Without a snapshot for the previous period the change honestly stays
    # empty: it must not be made up from current prices.
    change_24h_usd: Decimal | None = None
    change_24h_pct: Decimal | None = None
    change_7d_pct: Decimal | None = None
    # Coins for which no pair against a stablecoin was found.
    unpriced: list[str] = field(default_factory=list)
    has_accounts: bool = True


# --- Sync ---


async def sync_balances(
    session: AsyncSession,
    account: ExchangeAccount,
    adapter: ExchangeAdapter,
) -> int:
    """Refresh the balance snapshot of one connection.

    Coins that disappeared from the exchange response are deleted: otherwise a sold
    asset would hang in the portfolio forever.
    """
    entries = await adapter.fetch_balances()
    seen_asset_ids: set[int] = set()

    for entry in entries:
        asset = await market_service.get_or_create_asset(session, entry.asset)
        seen_asset_ids.add(asset.id)

        row = await _get_balance(session, account.id, asset.id)
        if row is None:
            row = Balance(exchange_account_id=account.id, asset_id=asset.id)
            session.add(row)

        row.free = entry.free
        row.locked = entry.locked
        row.total = entry.total
        row.synced_at = datetime.now(timezone.utc)

    existing = await session.execute(
        select(Balance).where(Balance.exchange_account_id == account.id)
    )
    for row in existing.scalars():
        if row.asset_id not in seen_asset_ids:
            await session.delete(row)

    await session.flush()
    return len(entries)


async def sync_trades(
    session: AsyncSession,
    account: ExchangeAccount,
    adapter: ExchangeAdapter,
    *,
    symbols: list[str],
    since: datetime | None = None,
) -> int:
    """Backfill the trade history.

    Re-running is safe: a trade is identified by the pair (connection, exchange id).
    """
    saved = 0

    for symbol in symbols:
        market = await market_service.get_market(session, account.exchange_id, symbol)
        if market is None:
            continue

        last_seen = since or await _last_trade_time(session, account.id, market.id)
        try:
            trades = await adapter.fetch_my_trades(symbol, since=last_seen)
        except Exception as exc:
            logger.warning("Could not fetch trades for %s: %s", symbol, exc)
            continue

        for info in trades:
            if await _trade_exists(session, account.id, info.external_id):
                continue

            fee_asset = None
            if info.fee_asset:
                fee_asset = await market_service.get_or_create_asset(session, info.fee_asset)

            session.add(
                Trade(
                    exchange_account_id=account.id,
                    market_id=market.id,
                    external_id=info.external_id,
                    order_id=info.order_id,
                    side=info.side,
                    price=info.price,
                    amount=info.amount,
                    cost=info.cost,
                    fee=info.fee,
                    fee_asset_id=fee_asset.id if fee_asset else None,
                    executed_at=info.executed_at,
                    raw=info.raw or None,
                )
            )
            saved += 1

    await session.flush()
    return saved


# --- Summary ---


async def build_summary(session: AsyncSession, user: User) -> PortfolioSummary:
    accounts = await _user_accounts(session, user)
    if not accounts:
        return PortfolioSummary(
            total_usd=Decimal(0), holdings=[], by_exchange={}, has_accounts=False
        )

    prices = await market_service.build_usd_price_map(session)
    changes = await _asset_change_map(session)

    account_ids = [a.id for a in accounts]
    exchange_by_account = {a.id: a.exchange_id for a in accounts}
    exchange_codes = await _exchange_codes(session)

    result = await session.execute(
        select(Balance, Asset.symbol)
        .join(Asset, Asset.id == Balance.asset_id)
        .where(Balance.exchange_account_id.in_(account_ids))
    )

    holdings: dict[str, Holding] = {}
    by_exchange: dict[str, Decimal] = {}
    unpriced: list[str] = []
    total = Decimal(0)

    for balance, symbol in result:
        if balance.total <= 0:
            continue

        usd = market_service.value_in_usd(prices, symbol, balance.total)
        exchange_code = exchange_codes.get(exchange_by_account[balance.exchange_account_id], "?")

        holding = holdings.get(symbol)
        if holding is None:
            holding = Holding(
                asset_symbol=symbol,
                total=Decimal(0),
                usd_value=None,
                change_24h_pct=changes.get(symbol),
            )
            holdings[symbol] = holding

        holding.total += balance.total
        holding.by_exchange[exchange_code] = holding.by_exchange.get(
            exchange_code, Decimal(0)
        ) + balance.total

        if usd is None:
            if symbol not in unpriced:
                unpriced.append(symbol)
            continue

        holding.usd_value = (holding.usd_value or Decimal(0)) + usd
        by_exchange[exchange_code] = by_exchange.get(exchange_code, Decimal(0)) + usd
        total += usd

    for holding in holdings.values():
        if holding.usd_value is not None and total > 0:
            holding.share_pct = holding.usd_value / total * Decimal(100)

    ordered = sorted(
        holdings.values(),
        key=lambda h: (h.usd_value is None, -(h.usd_value or Decimal(0))),
    )

    summary = PortfolioSummary(
        total_usd=total,
        holdings=ordered,
        by_exchange=dict(sorted(by_exchange.items(), key=lambda kv: -kv[1])),
        unpriced=unpriced,
    )
    await _fill_changes(session, user, summary)
    return summary


async def take_snapshot(session: AsyncSession, user: User) -> PortfolioSnapshot | None:
    """Record a point on the value chart.

    The breakdown is stored with the value: last month's chart must not be recomputed at
    today's prices.
    """
    summary = await build_summary(session, user)
    if not summary.has_accounts:
        return None

    snapshot = PortfolioSnapshot(
        user_id=user.id,
        total_usd=summary.total_usd,
        breakdown={
            "by_exchange": {k: money_str(v) for k, v in summary.by_exchange.items()},
            "by_asset": {
                h.asset_symbol: money_str(h.usd_value)
                for h in summary.holdings
                if h.usd_value is not None
            },
        },
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def history(
    session: AsyncSession, user: User, period: str = "7d"
) -> list[PortfolioSnapshot]:
    query = select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user.id)

    delta = PERIODS.get(period, PERIODS["7d"])
    if delta is not None:
        query = query.where(PortfolioSnapshot.captured_at >= datetime.now(timezone.utc) - delta)

    result = await session.execute(query.order_by(PortfolioSnapshot.captured_at))
    return list(result.scalars())


async def recent_trades(
    session: AsyncSession, user: User, *, limit: int = 100
) -> list[tuple[Trade, str, str]]:
    """The user's latest trades together with pair and exchange."""
    accounts = await _user_accounts(session, user)
    if not accounts:
        return []

    result = await session.execute(
        select(Trade, Market.symbol, Exchange.code)
        .join(Market, Market.id == Trade.market_id)
        .join(Exchange, Exchange.id == Market.exchange_id)
        .where(Trade.exchange_account_id.in_([a.id for a in accounts]))
        .order_by(Trade.executed_at.desc())
        .limit(limit)
    )
    return [(trade, symbol, code) for trade, symbol, code in result]


def money_str(value: Decimal) -> str:
    """Compact representation of an amount in JSON.

    Numeric(20, 8) returns 80000.00000000000000000000, and normalize() on its own gives
    8E+4 - neither belongs in a snapshot breakdown.
    """
    return format(value.normalize(), "f")


# --- Helpers ---


async def _user_accounts(session: AsyncSession, user: User) -> list[ExchangeAccount]:
    result = await session.execute(
        select(ExchangeAccount).where(ExchangeAccount.user_id == user.id)
    )
    return list(result.scalars())


async def _exchange_codes(session: AsyncSession) -> dict[int, str]:
    result = await session.execute(select(Exchange.id, Exchange.code))
    return {row_id: code for row_id, code in result}


async def _get_balance(session: AsyncSession, account_id: int, asset_id: int) -> Balance | None:
    result = await session.execute(
        select(Balance).where(
            Balance.exchange_account_id == account_id,
            Balance.asset_id == asset_id,
        )
    )
    return result.scalar_one_or_none()


async def _trade_exists(session: AsyncSession, account_id: int, external_id: str) -> bool:
    result = await session.execute(
        select(Trade.id).where(
            Trade.exchange_account_id == account_id,
            Trade.external_id == external_id,
        )
    )
    return result.first() is not None


async def _last_trade_time(
    session: AsyncSession, account_id: int, market_id: int
) -> datetime | None:
    result = await session.execute(
        select(Trade.executed_at)
        .where(Trade.exchange_account_id == account_id, Trade.market_id == market_id)
        .order_by(Trade.executed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _asset_change_map(session: AsyncSession) -> dict[str, Decimal]:
    """24-hour change per coin - from the quote snapshot."""
    base = Asset.__table__.alias("base_asset")
    quote = Asset.__table__.alias("quote_asset")

    result = await session.execute(
        select(base.c.symbol, MarketTicker.change_24h_pct)
        .select_from(MarketTicker)
        .join(Market, Market.id == MarketTicker.market_id)
        .join(base, base.c.id == Market.base_asset_id)
        .join(quote, quote.c.id == Market.quote_asset_id)
        .where(
            MarketTicker.change_24h_pct.is_not(None),
            quote.c.symbol.in_(market_service.STABLECOINS),
        )
        .order_by(Market.exchange_id)
    )

    changes: dict[str, Decimal] = {}
    for symbol, change in result:
        changes.setdefault(symbol, change)
    return changes


async def _fill_changes(
    session: AsyncSession, user: User, summary: PortfolioSummary
) -> None:
    """Compute the value change from stored snapshots."""
    day_ago = await _snapshot_before(session, user, timedelta(days=1))
    if day_ago is not None and day_ago.total_usd > 0:
        summary.change_24h_usd = summary.total_usd - day_ago.total_usd
        summary.change_24h_pct = summary.change_24h_usd / day_ago.total_usd * Decimal(100)

    week_ago = await _snapshot_before(session, user, timedelta(days=7))
    if week_ago is not None and week_ago.total_usd > 0:
        summary.change_7d_pct = (
            (summary.total_usd - week_ago.total_usd) / week_ago.total_usd * Decimal(100)
        )


async def _snapshot_before(
    session: AsyncSession, user: User, delta: timedelta
) -> PortfolioSnapshot | None:
    """The closest snapshot no later than the given moment."""
    moment = datetime.now(timezone.utc) - delta
    result = await session.execute(
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.user_id == user.id,
            PortfolioSnapshot.captured_at <= moment,
        )
        .order_by(PortfolioSnapshot.captured_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
