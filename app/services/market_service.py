"""Инструменты, котировки и оценка активов в долларах."""

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.base import ExchangeAdapter
from app.models import Asset, Exchange, Market, MarketTicker
from app.models.market import MARKET_TYPE_SPOT

logger = logging.getLogger(__name__)

# Стейблкоины считаем равными доллару. Отклонения от привязки бывают, но
# ловить их котировкой каждого стейбла к доллару — отдельная задача,
# которая на оценку портфеля влияет в пределах долей процента.
STABLECOINS = frozenset({"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USD"})


async def get_or_create_asset(session: AsyncSession, symbol: str, name: str | None = None) -> Asset:
    symbol = symbol.upper().strip()
    result = await session.execute(select(Asset).where(Asset.symbol == symbol))
    asset = result.scalar_one_or_none()
    if asset is not None:
        return asset

    asset = Asset(symbol=symbol, name=name or symbol)
    session.add(asset)
    await session.flush()
    return asset


async def get_market(
    session: AsyncSession, exchange_id: int, symbol: str
) -> Market | None:
    result = await session.execute(
        select(Market).where(
            Market.exchange_id == exchange_id,
            Market.symbol == symbol,
            Market.market_type == MARKET_TYPE_SPOT,
        )
    )
    return result.scalar_one_or_none()


async def sync_markets(
    session: AsyncSession,
    exchange: Exchange,
    adapter: ExchangeAdapter,
    *,
    only_quotes: set[str] | None = None,
) -> int:
    """Обновить список торговых пар биржи.

    only_quotes ограничивает набор валютой котировки: пар на бирже больше
    тысячи, а продукту нужны те, по которым считается портфель и графики.
    """
    markets = await adapter.fetch_markets()
    saved = 0

    for info in markets:
        if only_quotes and info.quote.upper() not in only_quotes:
            continue

        base = await get_or_create_asset(session, info.base)
        quote = await get_or_create_asset(session, info.quote)

        market = await get_market(session, exchange.id, info.symbol)
        if market is None:
            market = Market(
                exchange_id=exchange.id,
                base_asset_id=base.id,
                quote_asset_id=quote.id,
                symbol=info.symbol,
                raw_symbol=info.raw_symbol,
                market_type=info.market_type,
            )
            session.add(market)

        market.raw_symbol = info.raw_symbol
        market.price_precision = info.price_precision
        market.amount_precision = info.amount_precision
        market.min_amount = info.min_amount
        market.tick_size = info.tick_size
        market.is_active = True
        saved += 1

    await session.flush()
    return saved


async def update_tickers(
    session: AsyncSession,
    exchange: Exchange,
    adapter: ExchangeAdapter,
    *,
    symbols: list[str] | None = None,
) -> int:
    """Записать текущее состояние рынков в срез market_tickers.

    Пары и существующие срезы читаются двумя запросами, а не по одному
    на символ: у биржи их под тысячу, и обход по одному превращал бы
    обычное обновление котировок в сотни обращений к базе.
    """
    tickers = await adapter.fetch_tickers(symbols)
    if not tickers:
        return 0

    markets = await session.execute(
        select(Market.id, Market.symbol).where(
            Market.exchange_id == exchange.id,
            Market.market_type == MARKET_TYPE_SPOT,
        )
    )
    market_id_by_symbol = {symbol: market_id for market_id, symbol in markets}

    existing = await session.execute(
        select(MarketTicker).where(MarketTicker.market_id.in_(market_id_by_symbol.values()))
    )
    rows_by_market = {row.market_id: row for row in existing.scalars()}

    updated = 0
    for info in tickers:
        market_id = market_id_by_symbol.get(info.symbol)
        if market_id is None:
            continue

        row = rows_by_market.get(market_id)
        if row is None:
            row = MarketTicker(market_id=market_id)
            session.add(row)
            rows_by_market[market_id] = row

        row.last = info.last
        row.bid = info.bid
        row.ask = info.ask
        row.high_24h = info.high_24h
        row.low_24h = info.low_24h
        row.volume_24h = info.volume_24h
        row.change_24h_pct = info.change_24h_pct
        updated += 1

    await session.flush()
    return updated


async def build_usd_price_map(session: AsyncSession) -> dict[str, Decimal]:
    """Цена каждого актива в долларах по парам к стейблкоинам.

    Если по активу нет пары к стейблу, его в карте не будет — и оценка
    честно останется пустой вместо выдуманного числа.
    """
    prices: dict[str, Decimal] = {symbol: Decimal(1) for symbol in STABLECOINS}

    base = Asset.__table__.alias("base_asset")
    quote = Asset.__table__.alias("quote_asset")

    result = await session.execute(
        select(base.c.symbol, quote.c.symbol, MarketTicker.last, Market.exchange_id)
        .select_from(MarketTicker)
        .join(Market, Market.id == MarketTicker.market_id)
        .join(base, base.c.id == Market.base_asset_id)
        .join(quote, quote.c.id == Market.quote_asset_id)
        .where(MarketTicker.last.is_not(None))
        # Порядок фиксирован, чтобы оценка не прыгала между биржами от
        # запуска к запуску.
        .order_by(Market.exchange_id, base.c.symbol)
    )

    for base_symbol, quote_symbol, last, _exchange_id in result:
        if quote_symbol not in STABLECOINS or base_symbol in prices:
            continue
        if last is None or last <= 0:
            continue
        prices[base_symbol] = last

    return prices


def value_in_usd(
    prices: dict[str, Decimal], asset_symbol: str, amount: Decimal
) -> Decimal | None:
    price = prices.get(asset_symbol.upper())
    if price is None:
        return None
    return amount * price


async def get_ticker(session: AsyncSession, market_id: int) -> MarketTicker | None:
    return await session.get(MarketTicker, market_id)


async def compare_across_exchanges(session: AsyncSession, symbol: str) -> list[dict]:
    """Сопоставить одну пару на разных биржах: цена и спред.

    Сравнение из ТЗ строится именно так: одна пара на двух биржах — две
    записи в markets, и разница считается между ними.
    """
    result = await session.execute(
        select(Exchange.code, MarketTicker)
        .select_from(Market)
        .join(Exchange, Exchange.id == Market.exchange_id)
        .join(MarketTicker, MarketTicker.market_id == Market.id)
        .where(Market.symbol == symbol, Market.market_type == MARKET_TYPE_SPOT)
        .order_by(Exchange.sort_order)
    )

    rows = []
    for code, ticker in result:
        spread = None
        if ticker.bid is not None and ticker.ask is not None and ticker.bid > 0:
            spread = (ticker.ask - ticker.bid) / ticker.bid * Decimal(100)
        rows.append(
            {
                "exchange": code,
                "last": ticker.last,
                "bid": ticker.bid,
                "ask": ticker.ask,
                "spread_pct": spread,
                "change_24h_pct": ticker.change_24h_pct,
            }
        )
    return rows
