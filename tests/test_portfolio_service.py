from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select

from app.models import Balance, Exchange, PortfolioSnapshot, Trade
from app.services import exchange_keys_service as keys
from app.services import market_service, portfolio_service, user_service
from tests import fakes


@pytest_asyncio.fixture
async def setup(session):
    """Две биржи, пары к USDT, котировки и один пользователь с ключами."""
    bybit = Exchange(code="bybit", name="Bybit", sort_order=10)
    binance = Exchange(code="binance", name="Binance", sort_order=20)
    session.add_all([bybit, binance])
    await session.flush()

    market_adapter = fakes.FakeAdapter(
        markets=[
            fakes.market("BTC/USDT", "BTC", "USDT"),
            fakes.market("ETH/USDT", "ETH", "USDT"),
            fakes.market("SOL/USDT", "SOL", "USDT"),
        ],
        tickers=[
            fakes.ticker("BTC/USDT", "80000", "-1.5"),
            fakes.ticker("ETH/USDT", "2500", "2.0"),
            fakes.ticker("SOL/USDT", "150", "5.0"),
        ],
    )
    for exchange in (bybit, binance):
        await market_service.sync_markets(session, exchange, market_adapter)
        await market_service.update_tickers(session, exchange, market_adapter)

    user = await user_service.create_user(
        session, email="trader@example.com", password="trader-password-1"
    )
    await session.flush()

    accounts = {}
    for code in ("bybit", "binance"):
        accounts[code] = await keys.add_account(
            session,
            user,
            exchange_code=code,
            api_key=f"{code}-key-0001",
            api_secret=f"{code}-secret",
            adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
        )
    await session.commit()

    return {"user": user, "accounts": accounts, "bybit": bybit, "binance": binance}


# --- Синхронизация балансов ---


async def test_sync_balances_stores_amounts(session, setup):
    adapter = fakes.FakeAdapter(
        balances=[fakes.balance("BTC", "0.5", "0.4"), fakes.balance("USDT", "1000")]
    )

    count = await portfolio_service.sync_balances(session, setup["accounts"]["bybit"], adapter)
    await session.commit()

    assert count == 2
    rows = (await session.execute(select(Balance))).scalars().all()
    assert len(rows) == 2

    btc = next(r for r in rows if r.total == Decimal("0.5"))
    assert btc.free == Decimal("0.4")
    assert btc.locked == Decimal("0.1")


async def test_sold_asset_disappears_from_portfolio(session, setup):
    """Проданная монета не должна висеть в портфеле вечно."""
    account = setup["accounts"]["bybit"]

    await portfolio_service.sync_balances(
        session, account, fakes.FakeAdapter(balances=[fakes.balance("BTC", "1")])
    )
    await session.commit()
    assert len((await session.execute(select(Balance))).scalars().all()) == 1

    await portfolio_service.sync_balances(
        session, account, fakes.FakeAdapter(balances=[fakes.balance("USDT", "500")])
    )
    await session.commit()

    rows = (await session.execute(select(Balance))).scalars().all()
    assert len(rows) == 1
    assert rows[0].total == Decimal("500")


# --- Оценка ---


async def test_summary_values_and_shares(session, setup):
    await portfolio_service.sync_balances(
        session,
        setup["accounts"]["bybit"],
        fakes.FakeAdapter(balances=[fakes.balance("BTC", "0.5")]),
    )
    await portfolio_service.sync_balances(
        session,
        setup["accounts"]["binance"],
        fakes.FakeAdapter(balances=[fakes.balance("ETH", "4"), fakes.balance("USDT", "10000")]),
    )
    await session.commit()

    summary = await portfolio_service.build_summary(session, setup["user"])

    # 0.5 BTC * 80000 + 4 ETH * 2500 + 10000 USDT = 40000 + 10000 + 10000
    assert summary.total_usd == Decimal("60000")
    assert summary.by_exchange["bybit"] == Decimal("40000")
    assert summary.by_exchange["binance"] == Decimal("20000")

    btc = next(h for h in summary.holdings if h.asset_symbol == "BTC")
    assert btc.usd_value == Decimal("40000")
    assert btc.share_pct is not None
    assert btc.share_pct.quantize(Decimal("0.01")) == Decimal("66.67")
    assert btc.change_24h_pct == Decimal("-1.5")


async def test_same_asset_on_two_exchanges_is_merged(session, setup):
    for code, amount in (("bybit", "0.25"), ("binance", "0.75")):
        await portfolio_service.sync_balances(
            session,
            setup["accounts"][code],
            fakes.FakeAdapter(balances=[fakes.balance("BTC", amount)]),
        )
    await session.commit()

    summary = await portfolio_service.build_summary(session, setup["user"])
    btc = next(h for h in summary.holdings if h.asset_symbol == "BTC")

    assert btc.total == Decimal("1")
    assert btc.by_exchange == {"bybit": Decimal("0.25"), "binance": Decimal("0.75")}


async def test_asset_without_stable_pair_is_reported_not_guessed(session, setup):
    """Монету без пары к стейблу не оцениваем и говорим об этом прямо."""
    await portfolio_service.sync_balances(
        session,
        setup["accounts"]["bybit"],
        fakes.FakeAdapter(
            balances=[fakes.balance("BTC", "1"), fakes.balance("OBSCURE", "1000")]
        ),
    )
    await session.commit()

    summary = await portfolio_service.build_summary(session, setup["user"])

    assert summary.total_usd == Decimal("80000"), "неоценённое не попадает в итог"
    assert "OBSCURE" in summary.unpriced
    obscure = next(h for h in summary.holdings if h.asset_symbol == "OBSCURE")
    assert obscure.usd_value is None
    assert obscure.total == Decimal("1000")


async def test_stablecoins_count_as_dollars(session, setup):
    await portfolio_service.sync_balances(
        session,
        setup["accounts"]["bybit"],
        fakes.FakeAdapter(balances=[fakes.balance("USDT", "1234.56")]),
    )
    await session.commit()

    summary = await portfolio_service.build_summary(session, setup["user"])
    assert summary.total_usd == Decimal("1234.56")


async def test_no_accounts_gives_empty_summary(session):
    user = await user_service.create_user(
        session, email="empty@example.com", password="empty-password-1"
    )
    await session.commit()

    summary = await portfolio_service.build_summary(session, user)

    assert not summary.has_accounts
    assert summary.total_usd == Decimal(0)
    assert summary.holdings == []


# --- История стоимости ---


async def test_snapshot_records_breakdown(session, setup):
    await portfolio_service.sync_balances(
        session,
        setup["accounts"]["bybit"],
        fakes.FakeAdapter(balances=[fakes.balance("BTC", "1")]),
    )
    await session.commit()

    snapshot = await portfolio_service.take_snapshot(session, setup["user"])
    await session.commit()

    assert snapshot.total_usd == Decimal("80000")
    assert snapshot.breakdown["by_exchange"]["bybit"] == "80000"
    assert snapshot.breakdown["by_asset"]["BTC"] == "80000"


async def test_change_is_computed_from_snapshots(session, setup):
    await portfolio_service.sync_balances(
        session,
        setup["accounts"]["bybit"],
        fakes.FakeAdapter(balances=[fakes.balance("BTC", "1")]),
    )
    await session.commit()

    session.add(
        PortfolioSnapshot(
            user_id=setup["user"].id,
            total_usd=Decimal("64000"),
            captured_at=datetime.now(timezone.utc) - timedelta(days=1, hours=1),
        )
    )
    await session.commit()

    summary = await portfolio_service.build_summary(session, setup["user"])

    assert summary.change_24h_usd == Decimal("16000")
    assert summary.change_24h_pct == Decimal("25")


async def test_change_stays_empty_without_history(session, setup):
    """Без снимка за прошлый период изменение не выдумывается."""
    await portfolio_service.sync_balances(
        session,
        setup["accounts"]["bybit"],
        fakes.FakeAdapter(balances=[fakes.balance("BTC", "1")]),
    )
    await session.commit()

    summary = await portfolio_service.build_summary(session, setup["user"])

    assert summary.change_24h_pct is None
    assert summary.change_7d_pct is None


# --- Сделки ---


async def test_sync_trades_is_idempotent(session, setup):
    account = setup["accounts"]["bybit"]
    adapter = fakes.FakeAdapter(
        trades=[
            fakes.trade("t-1", "BTC/USDT", "buy", "70000", "0.1"),
            fakes.trade("t-2", "BTC/USDT", "sell", "80000", "0.05"),
        ]
    )

    first = await portfolio_service.sync_trades(session, account, adapter, symbols=["BTC/USDT"])
    await session.commit()
    second = await portfolio_service.sync_trades(session, account, adapter, symbols=["BTC/USDT"])
    await session.commit()

    assert first == 2
    assert second == 0, "повторная синхронизация не должна плодить дубли"
    assert len((await session.execute(select(Trade))).scalars().all()) == 2


async def test_trades_are_listed_with_symbol_and_exchange(session, setup):
    await portfolio_service.sync_trades(
        session,
        setup["accounts"]["binance"],
        fakes.FakeAdapter(trades=[fakes.trade("t-9", "ETH/USDT", "buy", "2400", "2")]),
        symbols=["ETH/USDT"],
    )
    await session.commit()

    rows = await portfolio_service.recent_trades(session, setup["user"])

    assert len(rows) == 1
    trade, symbol, exchange_code = rows[0]
    assert symbol == "ETH/USDT"
    assert exchange_code == "binance"
    assert trade.cost == Decimal("4800")


# --- Сравнение бирж ---


async def test_compare_across_exchanges_reports_spread(session, setup):
    rows = await market_service.compare_across_exchanges(session, "BTC/USDT")

    assert {r["exchange"] for r in rows} == {"bybit", "binance"}
    assert all(r["last"] == Decimal("80000") for r in rows)
    assert all(r["spread_pct"] == Decimal(0) for r in rows)
