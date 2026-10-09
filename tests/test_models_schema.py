"""Schema integrity checks.

They catch typos in relationships and foreign keys at test time, not during the first
migration on a live database.
"""

from decimal import Decimal

from sqlalchemy import inspect, select

from app import models
from app.models import Asset, Exchange, Market, Timeframe


async def test_all_tables_created(engine):
    """The metadata creates real tables without conflicts."""
    def _tables(conn):
        return set(inspect(conn).get_table_names())

    async with engine.connect() as conn:
        tables = await conn.run_sync(_tables)

    expected = {
        "users",
        "user_recovery_codes",
        "invites",
        "login_events",
        "audit_log",
        "exchanges",
        "exchange_accounts",
        "assets",
        "markets",
        "timeframes",
        "candles",
        "market_tickers",
        "global_stats",
        "balances",
        "portfolio_snapshots",
        "trades",
        "positions",
        "watchlist_items",
        "signal_rules",
        "signals",
        "signal_outcomes",
        "alert_types",
        "alerts",
        "alert_triggers",
        "notifications",
        "notification_settings",
        "strategies",
        "bot_orders",
        "bot_journal",
        "risk_state",
    }
    assert expected <= tables, f"не созданы таблицы: {expected - tables}"


async def test_all_relationships_resolve():
    """All string references in relationship() point to existing classes.

    SQLAlchemy resolves them lazily, so a typo only surfaces on first access - here we
    force them all to resolve at once.
    """
    for name in models.__all__:
        obj = getattr(models, name)
        mapper = getattr(obj, "__mapper__", None)
        if mapper is None:
            continue
        for rel in mapper.relationships:
            assert rel.mapper is not None, f"{name}.{rel.key} не разрешается"


async def test_decimal_survives_roundtrip(session):
    """Quantities don't lose precision on write and read."""
    exchange = Exchange(code="bybit", name="Bybit")
    base = Asset(symbol="BTC", name="Bitcoin")
    quote = Asset(symbol="USDT", name="Tether")
    session.add_all([exchange, base, quote])
    await session.flush()

    market = Market(
        exchange_id=exchange.id,
        base_asset_id=base.id,
        quote_asset_id=quote.id,
        symbol="BTC/USDT",
        raw_symbol="BTCUSDT",
        min_amount=Decimal("0.000001"),
    )
    session.add(market)
    await session.commit()

    stored = (await session.execute(select(Market))).scalar_one()
    assert stored.min_amount == Decimal("0.000001")


async def test_timeframe_code_is_unique(session):
    session.add(Timeframe(code="1m", label="1 минута", seconds=60))
    await session.commit()

    session.add(Timeframe(code="1m", label="дубль", seconds=60))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
    else:
        raise AssertionError("дубликат кода таймфрейма прошёл в базу")
