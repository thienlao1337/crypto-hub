from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select

from app.models import Balance, Exchange, Position, Trade
from app.services import exchange_keys_service as keys
from app.services import market_service, portfolio_service, position_service, user_service
from tests import fakes

BASE_ID = 1
QUOTE_ID = 2
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_trade(
    side: str,
    price: str,
    amount: str,
    *,
    minute: int = 0,
    fee: str | None = None,
    fee_asset_id: int | None = None,
) -> Trade:
    """Сделка в памяти: walk_trades работает без базы."""
    price_dec = Decimal(price)
    amount_dec = Decimal(amount)
    return Trade(
        exchange_account_id=1,
        market_id=1,
        external_id=f"t{minute}",
        side=side,
        price=price_dec,
        amount=amount_dec,
        cost=price_dec * amount_dec,
        fee=Decimal(fee) if fee is not None else None,
        fee_asset_id=fee_asset_id,
        executed_at=START + timedelta(minutes=minute),
    )


def walk(trades: list[Trade]) -> position_service.WalkResult:
    return position_service.walk_trades(
        trades, base_asset_id=BASE_ID, quote_asset_id=QUOTE_ID
    )


# --- Средняя цена входа ---


def test_two_buys_average_out():
    result = walk([make_trade("buy", "10000", "1"), make_trade("buy", "20000", "1", minute=1)])

    assert result.amount == Decimal(2)
    assert result.entry_price == Decimal(15000)
    assert result.complete is True
    assert result.opened_at == START


def test_partial_sell_keeps_entry_price():
    """Продажа части позиции не меняет среднюю цену оставшейся."""
    result = walk(
        [
            make_trade("buy", "10000", "1"),
            make_trade("buy", "20000", "1", minute=1),
            make_trade("sell", "20000", "0.5", minute=2),
        ]
    )

    assert result.amount == Decimal("1.5")
    assert result.entry_price == Decimal(15000)
    # Продано по 20000 то, что вошло по 15000: 0.5 × 5000.
    assert result.realized[2] == Decimal(2500)


def test_realized_pnl_only_on_sells():
    result = walk([make_trade("buy", "100", "1"), make_trade("sell", "80", "1", minute=1)])

    assert result.realized[0] is None
    assert result.realized[1] == Decimal(-20)
    assert result.amount == Decimal(0)


def test_closed_position_reopens_from_scratch():
    """Полное закрытие обнуляет базис: следующая покупка начинает заново."""
    result = walk(
        [
            make_trade("buy", "100", "1"),
            make_trade("sell", "200", "1", minute=1),
            make_trade("buy", "300", "2", minute=2),
        ]
    )

    assert result.amount == Decimal(2)
    assert result.entry_price == Decimal(300)
    assert result.opened_at == START + timedelta(minutes=2)


# --- Неполная история ---


def test_sell_without_buy_marks_history_incomplete():
    """Биржа отдаёт ограниченный период — покупка могла остаться за ним."""
    result = walk([make_trade("sell", "100", "1")])

    assert result.complete is False
    assert result.realized == [None]
    assert result.amount == Decimal(0)


def test_oversized_sell_closes_position_and_flags_it():
    result = walk([make_trade("buy", "100", "1"), make_trade("sell", "150", "3", minute=1)])

    assert result.complete is False
    assert result.amount == Decimal(0)
    # Учтена только известная часть: 1 монета, а не три.
    assert result.realized[1] == Decimal(50)


# --- Комиссии ---


def test_quote_fee_raises_cost_basis():
    result = walk([make_trade("buy", "10000", "1", fee="10", fee_asset_id=QUOTE_ID)])

    assert result.entry_price == Decimal(10010)


def test_base_fee_reduces_received_amount():
    result = walk([make_trade("buy", "10000", "1", fee="0.001", fee_asset_id=BASE_ID)])

    assert result.amount == Decimal("0.999")
    assert result.cost == Decimal(10000)


def test_third_asset_fee_is_ignored():
    """Комиссию в BNB не по чему пересчитать — молча подставлять курс нельзя."""
    result = walk([make_trade("buy", "10000", "1", fee="0.5", fee_asset_id=99)])

    assert result.amount == Decimal(1)
    assert result.entry_price == Decimal(10000)


# --- Пересборка позиций в базе ---


@pytest_asyncio.fixture
async def setup(session):
    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    session.add(exchange)
    await session.flush()

    adapter = fakes.FakeAdapter(
        markets=[fakes.market("BTC/USDT", "BTC", "USDT")],
        tickers=[fakes.ticker("BTC/USDT", "80000")],
    )
    await market_service.sync_markets(session, exchange, adapter)
    await market_service.update_tickers(session, exchange, adapter)

    user = await user_service.create_user(
        session, email="pos@example.com", password="pos-password-1"
    )
    await session.flush()

    account = await keys.add_account(
        session,
        user,
        exchange_code="bybit",
        api_key="bybit-key-0001",
        api_secret="bybit-secret",
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )
    await session.commit()

    return {"user": user, "account": account, "exchange": exchange}


async def load_trades(session, account, trades):
    adapter = fakes.FakeAdapter(trades=trades)
    await portfolio_service.sync_trades(
        session, account, adapter, symbols=["BTC/USDT"]
    )
    await session.flush()


async def test_rebuild_creates_open_position(session, setup):
    account = setup["account"]
    await load_trades(
        session,
        account,
        [
            fakes.trade("1", "BTC/USDT", "buy", "60000", "0.4", executed_at=START),
            fakes.trade(
                "2", "BTC/USDT", "buy", "60000", "0.1",
                executed_at=START + timedelta(hours=1),
            ),
        ],
    )

    count = await position_service.rebuild_positions(session, account)
    await session.commit()

    assert count == 1
    position = (await session.execute(select(Position))).scalar_one()
    assert position.amount == Decimal("0.5")
    assert position.entry_price == Decimal(60000)
    assert position.is_open is True
    assert position.cost_basis_complete is True


async def test_rebuild_writes_realized_pnl_into_trades(session, setup):
    account = setup["account"]
    await load_trades(
        session,
        account,
        [
            fakes.trade("1", "BTC/USDT", "buy", "60000", "1", executed_at=START),
            fakes.trade(
                "2", "BTC/USDT", "sell", "70000", "0.5",
                executed_at=START + timedelta(hours=1),
            ),
        ],
    )

    await position_service.rebuild_positions(session, account)
    await session.commit()

    sell = (
        await session.execute(select(Trade).where(Trade.external_id == "2"))
    ).scalar_one()
    buy = (
        await session.execute(select(Trade).where(Trade.external_id == "1"))
    ).scalar_one()

    assert sell.realized_pnl == Decimal(5000)
    assert buy.realized_pnl is None


async def test_full_close_marks_position_closed(session, setup):
    """Закрытая позиция остаётся строкой с is_open = False, а не пропадает."""
    account = setup["account"]
    await load_trades(
        session,
        account,
        [fakes.trade("1", "BTC/USDT", "buy", "60000", "1", executed_at=START)],
    )
    await position_service.rebuild_positions(session, account)
    await position_service.mark_positions(session)
    await session.commit()

    await load_trades(
        session,
        account,
        [
            fakes.trade("1", "BTC/USDT", "buy", "60000", "1", executed_at=START),
            fakes.trade(
                "2", "BTC/USDT", "sell", "70000", "1",
                executed_at=START + timedelta(hours=1),
            ),
        ],
    )
    count = await position_service.rebuild_positions(session, account)
    await session.commit()

    assert count == 0
    position = (await session.execute(select(Position))).scalar_one()
    assert position.is_open is False
    assert position.amount == Decimal(0)
    assert position.unrealized_pnl is None


async def test_never_open_position_creates_no_row(session, setup):
    """Пара, вся история которой сводится в ноль, строку не заводит."""
    account = setup["account"]
    await load_trades(
        session,
        account,
        [
            fakes.trade("1", "BTC/USDT", "buy", "60000", "1", executed_at=START),
            fakes.trade(
                "2", "BTC/USDT", "sell", "70000", "1",
                executed_at=START + timedelta(hours=1),
            ),
        ],
    )

    await position_service.rebuild_positions(session, account)
    await session.commit()

    assert (await session.execute(select(Position))).scalars().all() == []


async def test_rebuild_is_idempotent(session, setup):
    """Повторный прогон не должен ни удваивать позицию, ни плодить строки."""
    account = setup["account"]
    await load_trades(
        session,
        account,
        [fakes.trade("1", "BTC/USDT", "buy", "60000", "0.5", executed_at=START)],
    )

    await position_service.rebuild_positions(session, account)
    await session.commit()
    await position_service.rebuild_positions(session, account)
    await session.commit()

    rows = (await session.execute(select(Position))).scalars().all()
    assert len(rows) == 1
    assert rows[0].amount == Decimal("0.5")


async def test_balance_larger_than_history_flags_position(session, setup):
    """На бирже монет больше, чем объясняют сделки — базис неполный."""
    account = setup["account"]
    await load_trades(
        session,
        account,
        [fakes.trade("1", "BTC/USDT", "buy", "60000", "0.5", executed_at=START)],
    )

    balances = fakes.FakeAdapter(balances=[fakes.balance("BTC", "2")])
    await portfolio_service.sync_balances(session, account, balances)

    await position_service.rebuild_positions(session, account)
    await session.commit()

    position = (await session.execute(select(Position))).scalar_one()
    assert position.cost_basis_complete is False


async def test_balance_matching_history_stays_complete(session, setup):
    account = setup["account"]
    await load_trades(
        session,
        account,
        [fakes.trade("1", "BTC/USDT", "buy", "60000", "0.5", executed_at=START)],
    )

    balances = fakes.FakeAdapter(balances=[fakes.balance("BTC", "0.5")])
    await portfolio_service.sync_balances(session, account, balances)

    await position_service.rebuild_positions(session, account)
    await session.commit()

    position = (await session.execute(select(Position))).scalar_one()
    assert position.cost_basis_complete is True


# --- Переоценка и вывод ---


async def test_mark_positions_computes_unrealized_pnl(session, setup):
    account = setup["account"]
    await load_trades(
        session,
        account,
        [fakes.trade("1", "BTC/USDT", "buy", "60000", "0.5", executed_at=START)],
    )
    await position_service.rebuild_positions(session, account)

    updated = await position_service.mark_positions(session)
    await session.commit()

    assert updated == 1
    position = (await session.execute(select(Position))).scalar_one()
    assert position.mark_price == Decimal(80000)
    # (80000 − 60000) × 0.5
    assert position.unrealized_pnl == Decimal(10000)


async def test_list_positions_returns_percent_and_totals(session, setup):
    account = setup["account"]
    await load_trades(
        session,
        account,
        [fakes.trade("1", "BTC/USDT", "buy", "60000", "0.5", executed_at=START)],
    )
    await position_service.rebuild_positions(session, account)
    await position_service.mark_positions(session)
    await session.commit()

    views = await position_service.list_positions(session, setup["user"])

    assert len(views) == 1
    view = views[0]
    assert view.symbol == "BTC/USDT"
    assert view.exchange == "bybit"
    assert view.cost_usd == Decimal(30000)
    assert view.value_usd == Decimal(40000)
    assert view.unrealized_pct.quantize(Decimal("0.01")) == Decimal("33.33")
    assert position_service.total_unrealized(views) == Decimal(10000)


async def test_total_unrealized_without_prices_is_none(session, setup):
    """Пустой список и «нечего считать» — разные состояния, не ноль."""
    assert position_service.total_unrealized([]) is None


async def test_closed_position_is_not_listed(session, setup):
    account = setup["account"]
    await load_trades(
        session,
        account,
        [
            fakes.trade("1", "BTC/USDT", "buy", "60000", "1", executed_at=START),
            fakes.trade(
                "2", "BTC/USDT", "sell", "70000", "1",
                executed_at=START + timedelta(hours=1),
            ),
        ],
    )
    await position_service.rebuild_positions(session, account)
    await session.commit()

    assert await position_service.list_positions(session, setup["user"]) == []


async def test_balance_row_untouched_by_rebuild(session, setup):
    """Пересборка позиций не должна трогать срез балансов."""
    account = setup["account"]
    balances = fakes.FakeAdapter(balances=[fakes.balance("BTC", "0.5")])
    await portfolio_service.sync_balances(session, account, balances)
    await load_trades(
        session,
        account,
        [fakes.trade("1", "BTC/USDT", "buy", "60000", "0.5", executed_at=START)],
    )

    await position_service.rebuild_positions(session, account)
    await session.commit()

    row = (await session.execute(select(Balance))).scalar_one()
    assert row.total == Decimal("0.5")
