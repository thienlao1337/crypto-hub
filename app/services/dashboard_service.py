"""Home screen data.

Collects what other services have already computed and adds market-wide metrics. No
exchange calls: the dashboard must open instantly, and freshness is the background
process's job.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Asset,
    Exchange,
    GlobalStats,
    Market,
    MarketTicker,
    Notification,
    User,
)
from app.models.market import MARKET_TYPE_SPOT
from app.providers import coingecko, fear_greed

# Tiny coins with near-zero turnover produce wild percentages and push
# everything meaningful out of the top.
MIN_TURNOVER_USD = Decimal(1_000_000)
TOP_SIZE = 5


@dataclass
class Mover:
    symbol: str
    exchange: str
    slug: str
    last: Decimal | None
    change_24h_pct: Decimal | None


async def latest_global_stats(session: AsyncSession) -> GlobalStats | None:
    result = await session.execute(
        select(GlobalStats).order_by(GlobalStats.captured_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def top_movers(session: AsyncSession) -> dict[str, list[Mover]]:
    """Top gainers and losers over 24 hours."""
    base_asset = Asset.__table__.alias("base_asset")

    rows = (
        await session.execute(
            select(
                Market.symbol,
                Exchange.code,
                base_asset.c.symbol,
                MarketTicker.last,
                MarketTicker.change_24h_pct,
                MarketTicker.quote_volume_24h,
            )
            .select_from(Market)
            .join(Exchange, Exchange.id == Market.exchange_id)
            .join(base_asset, base_asset.c.id == Market.base_asset_id)
            .join(MarketTicker, MarketTicker.market_id == Market.id)
            .where(
                Market.market_type == MARKET_TYPE_SPOT,
                Market.is_active.is_(True),
                MarketTicker.change_24h_pct.is_not(None),
                MarketTicker.quote_volume_24h >= MIN_TURNOVER_USD,
            )
        )
    ).all()

    # One coin trades in several pairs and on two exchanges - DASH/USDT,
    # DASH/USDC and so on. It should appear in the top once: keep the pair with
    # the highest turnover.
    best: dict[str, tuple[Decimal, Mover]] = {}
    for symbol, exchange_code, asset_symbol, last, change, turnover in rows:
        turnover = turnover or Decimal(0)
        current = best.get(asset_symbol)
        if current is not None and current[0] >= turnover:
            continue

        best[asset_symbol] = (
            turnover,
            Mover(
                symbol=symbol,
                exchange=exchange_code,
                slug=symbol.replace("/", "-"),
                last=last,
                change_24h_pct=change,
            ),
        )

    movers = [mover for _turnover, mover in best.values()]
    gainers = sorted(movers, key=lambda m: m.change_24h_pct, reverse=True)[:TOP_SIZE]
    losers = sorted(movers, key=lambda m: m.change_24h_pct)[:TOP_SIZE]
    return {"gainers": gainers, "losers": losers}


async def recent_events(
    session: AsyncSession, user: User, *, limit: int = 8
) -> list[Notification]:
    result = await session.execute(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def refresh_global_stats(session: AsyncSession) -> GlobalStats | None:
    """Fetch market metrics and the Fear & Greed index.

    Sources are polled independently: one being unavailable must not deprive the
    dashboard of the other's data.
    """
    market = await coingecko.fetch_global()
    index = await fear_greed.fetch()

    if market is None and index is None:
        return None

    snapshot = GlobalStats(
        total_market_cap_usd=market.total_market_cap_usd if market else None,
        total_volume_24h_usd=market.total_volume_24h_usd if market else None,
        market_cap_change_24h_pct=market.market_cap_change_24h_pct if market else None,
        btc_dominance=market.btc_dominance if market else None,
        eth_dominance=market.eth_dominance if market else None,
        fng_value=index.value if index else None,
        fng_label=index.label if index else None,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot
