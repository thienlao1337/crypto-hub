"""Данные главного экрана.

Собирает то, что уже посчитано другими сервисами, и добавляет
общерыночные показатели. Никаких обращений к биржам: дашборд должен
открываться мгновенно, а свежесть обеспечивает фоновый процесс.
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

# Мелочь с околонулевым оборотом даёт дикие проценты и вытесняет из
# топа всё осмысленное.
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
    """Топ растущих и падающих за сутки."""
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

    # Одна монета торгуется несколькими парами и на двух биржах —
    # DASH/USDT, DASH/USDC и так далее. В топе она нужна один раз:
    # оставляем пару с наибольшим оборотом.
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
    """Снять показатели рынка и индекс страха и жадности.

    Источники опрашиваются независимо: недоступность одного не должна
    лишать дашборд данных другого.
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
