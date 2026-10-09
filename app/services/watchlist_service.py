"""Watchlist.

It isn't just a convenience: the background process uses it to decide which pairs to
download candles for, compute signals on and backfill trade history for. Downloading
everything isn't an option - there are over a thousand pairs.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Exchange, Market, MarketTicker, User, WatchlistItem

MAX_ITEMS = 50


class WatchlistError(Exception):
    pass


async def list_items(session: AsyncSession, user: User) -> list[dict]:
    result = await session.execute(
        select(WatchlistItem, Market.symbol, Exchange.code, MarketTicker)
        .join(Market, Market.id == WatchlistItem.market_id)
        .join(Exchange, Exchange.id == Market.exchange_id)
        .outerjoin(MarketTicker, MarketTicker.market_id == Market.id)
        .where(WatchlistItem.user_id == user.id)
        .order_by(WatchlistItem.sort_order, WatchlistItem.id)
    )
    return [
        {
            "item": item,
            "symbol": symbol,
            "exchange": code,
            "slug": symbol.replace("/", "-"),
            "ticker": ticker,
        }
        for item, symbol, code, ticker in result
    ]


async def market_ids(session: AsyncSession, user: User) -> list[int]:
    result = await session.execute(
        select(WatchlistItem.market_id).where(WatchlistItem.user_id == user.id)
    )
    return [market_id for (market_id,) in result]


async def is_watched(session: AsyncSession, user: User, market_id: int) -> bool:
    result = await session.execute(
        select(WatchlistItem.id).where(
            WatchlistItem.user_id == user.id,
            WatchlistItem.market_id == market_id,
        )
    )
    return result.first() is not None


async def add(session: AsyncSession, user: User, market_id: int) -> WatchlistItem | None:
    """Add a pair. Adding it again breaks nothing."""
    existing = await session.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user.id,
            WatchlistItem.market_id == market_id,
        )
    )
    item = existing.scalar_one_or_none()
    if item is not None:
        return item

    count = await session.scalar(
        select(func.count()).select_from(WatchlistItem).where(WatchlistItem.user_id == user.id)
    )
    if int(count or 0) >= MAX_ITEMS:
        # The limit isn't cosmetic: for every pair the background process
        # downloads candles and computes indicators.
        raise WatchlistError(f"В списке уже {MAX_ITEMS} пар — больше не добавить.")

    item = WatchlistItem(user_id=user.id, market_id=market_id, sort_order=int(count or 0))
    session.add(item)
    await session.flush()
    return item


async def remove(session: AsyncSession, user: User, market_id: int) -> None:
    result = await session.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user.id,
            WatchlistItem.market_id == market_id,
        )
    )
    item = result.scalar_one_or_none()
    if item is not None:
        await session.delete(item)
        await session.flush()


async def toggle(session: AsyncSession, user: User, market_id: int) -> bool:
    """Toggle watching. Returns the new state."""
    if await is_watched(session, user, market_id):
        await remove(session, user, market_id)
        return False
    await add(session, user, market_id)
    return True
