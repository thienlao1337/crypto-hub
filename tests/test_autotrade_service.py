"""Проверки автотрейдинга.

Здесь ошибка стоит денег пользователя, поэтому проверяется в первую
очередь то, что бот НЕ делает: не торгует при выключенном рубильнике, не
уходит в live без подтверждения, не продолжает после дневного лимита
убытка.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models import (
    BotJournalEntry,
    BotOrder,
    Exchange,
    MarketTicker,
    Signal,
    SignalRule,
    Timeframe,
)
from app.models.exchange import KEY_STATUS_OK
from app.models.trading import MODE_LIVE, MODE_PAPER, MODE_TESTNET
from app.services import autotrade_service as auto
from app.services import exchange_keys_service as keys
from app.services import market_service, portfolio_service, user_service
from tests import fakes

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def setup(session, monkeypatch):
    """Пользователь с депозитом 10 000 USDT и стратегией на BTC/USDT."""
    monkeypatch.setattr(auto.settings, "autotrade_enabled", True, raising=False)

    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    session.add(exchange)
    timeframe = Timeframe(code="1h", label="1 час", seconds=3600, sort_order=40)
    session.add(timeframe)
    await session.flush()

    await market_service.sync_markets(
        session, exchange, fakes.FakeAdapter(markets=[fakes.market("BTC/USDT", "BTC", "USDT")])
    )
    market = await market_service.get_market(session, exchange.id, "BTC/USDT")
    session.add(MarketTicker(market_id=market.id, last=Decimal(80_000)))

    user = await user_service.create_user(
        session, email="trader@example.com", password="trader-password-1"
    )
    await session.flush()

    account = await keys.add_account(
        session, user,
        exchange_code="bybit", api_key="key-0001", api_secret="secret",
        want_trading=True,
        adapter_factory=fakes.factory_for(
            fakes.FakeAdapter(key_check=fakes.KeyCheck(
                is_valid=True, can_trade=True, permissions_known=True
            ))
        ),
    )

    await portfolio_service.sync_balances(
        session, account, fakes.FakeAdapter(balances=[fakes.balance("USDT", "10000")])
    )

    rule = SignalRule(
        name="EMA + RSI", timeframe_id=timeframe.id, market_id=market.id,
        config={}, evaluation_horizon_minutes=60,
    )
    session.add(rule)
    await session.flush()

    strategy = await auto.create_strategy(
        session, user,
        name="Проба",
        signal_rule_id=rule.id,
        exchange_account_id=account.id,
        market_id=market.id,
        position_size_pct=Decimal(10),
        max_pct_per_trade=Decimal(20),
        daily_loss_limit_pct=Decimal(5),
        stop_loss_pct=Decimal(2),
        take_profit_pct=Decimal(4),
    )
    await session.commit()

    return {
        "user": user, "account": account, "market": market,
        "rule": rule, "strategy": strategy, "timeframe": timeframe,
    }


def make_signal(setup, direction: str = "buy") -> Signal:
    return Signal(
        rule_id=setup["rule"].id,
        market_id=setup["market"].id,
        timeframe_id=setup["timeframe"].id,
        direction=direction,
        price=Decimal(80_000),
        reason="проверка",
        indicators={},
        candle_time=NOW,
    )


# --- Ограничения параметров ---


@pytest.mark.parametrize(
    "params",
    [
        {"position_size_pct": Decimal(0)},
        {"position_size_pct": Decimal(80)},
        {"max_pct_per_trade": Decimal(90)},
        {"daily_loss_limit_pct": Decimal(0)},
        {"daily_loss_limit_pct": Decimal(90)},
        {"stop_loss_pct": Decimal(99)},
        {"take_profit_pct": Decimal(0)},
        # Позиция больше собственного потолка — противоречие в настройках.
        {"position_size_pct": Decimal(30), "max_pct_per_trade": Decimal(10)},
    ],
)
def test_unsafe_parameters_rejected(params):
    base = {
        "position_size_pct": Decimal(10),
        "max_pct_per_trade": Decimal(20),
        "daily_loss_limit_pct": Decimal(5),
        "stop_loss_pct": Decimal(2),
        "take_profit_pct": Decimal(4),
    }
    base.update(params)

    with pytest.raises(auto.AutotradeError):
        auto.validate(**base)


def test_reasonable_parameters_accepted():
    auto.validate(
        position_size_pct=Decimal(5),
        max_pct_per_trade=Decimal(10),
        daily_loss_limit_pct=Decimal(3),
        stop_loss_pct=None,
        take_profit_pct=None,
    )


# --- Режимы ---


async def test_strategy_starts_paper_and_stopped(session, setup):
    """Созданная стратегия не должна ничего делать сама по себе."""
    strategy = setup["strategy"]

    assert strategy.mode == MODE_PAPER
    assert not strategy.is_active
    assert strategy.live_confirmed_at is None


async def test_live_requires_exchange_confirmed_trading(session, setup):
    account = setup["account"]
    account.allow_trading = False
    await session.commit()

    with pytest.raises(auto.AutotradeError) as exc:
        await auto.set_mode(session, setup["user"], setup["strategy"], MODE_LIVE)

    assert "права на торговлю" in str(exc.value)
    assert setup["strategy"].mode == MODE_PAPER


async def test_live_rejected_for_testnet_key(session, setup):
    setup["account"].is_testnet = True
    await session.commit()

    with pytest.raises(auto.AutotradeError):
        await auto.set_mode(session, setup["user"], setup["strategy"], MODE_LIVE)


async def test_live_records_confirmation_and_stops_strategy(session, setup):
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.commit()

    await auto.set_mode(session, setup["user"], strategy, MODE_LIVE)
    await session.commit()

    assert strategy.mode == MODE_LIVE
    assert strategy.live_confirmed_at is not None
    # Смена режима останавливает: запуск — отдельное осознанное действие.
    assert not strategy.is_active


async def test_leaving_live_clears_confirmation(session, setup):
    strategy = setup["strategy"]
    await auto.set_mode(session, setup["user"], strategy, MODE_LIVE)
    await auto.set_mode(session, setup["user"], strategy, MODE_PAPER)
    await session.commit()

    assert strategy.live_confirmed_at is None


async def test_cannot_start_live_without_confirmation(session, setup):
    strategy = setup["strategy"]
    # Режим подменяем напрямую, минуя проверки — так мог бы выглядеть
    # испорченный ряд в базе.
    strategy.mode = MODE_LIVE
    strategy.live_confirmed_at = None
    await session.commit()

    with pytest.raises(auto.AutotradeError):
        await auto.set_active(session, strategy, True)


async def test_unknown_mode_rejected(session, setup):
    with pytest.raises(auto.AutotradeError):
        await auto.set_mode(session, setup["user"], setup["strategy"], "что-то своё")


# --- Расчёт размера ---


def test_position_size_uses_share_of_equity(setup=None):
    strategy = type("S", (), {
        "position_size_pct": Decimal(10),
        "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "buy"})()

    decision = auto.decide(
        strategy=strategy, signal=signal,
        equity_usd=Decimal(10_000), price=Decimal(80_000),
    )

    assert decision.action == "order"
    # 10% от 10 000 = 1 000 USD, при цене 80 000 это 0.0125 BTC.
    assert decision.amount == Decimal("0.0125")


def test_position_size_capped_by_max_per_trade():
    """Потолок на сделку сильнее заданного размера позиции."""
    strategy = type("S", (), {
        "position_size_pct": Decimal(30),
        "max_pct_per_trade": Decimal(5),
    })()
    signal = type("Sig", (), {"direction": "buy"})()

    decision = auto.decide(
        strategy=strategy, signal=signal,
        equity_usd=Decimal(10_000), price=Decimal(100),
    )

    assert decision.amount == Decimal(5), "5% от 10 000 при цене 100"


def test_no_order_without_equity_or_price():
    strategy = type("S", (), {
        "position_size_pct": Decimal(10), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "buy"})()

    assert auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(0), price=Decimal(100)
    ).action == "skip"
    assert auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(100), price=Decimal(0)
    ).action == "skip"


# --- Исполнение ---


async def test_global_switch_blocks_everything(session, setup, monkeypatch):
    """Выключенный рубильник важнее любых настроек стратегии."""
    monkeypatch.setattr(auto.settings, "autotrade_enabled", False, raising=False)
    strategy = setup["strategy"]
    strategy.is_active = True
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    order = await auto.execute(session, strategy, signal)
    await session.commit()

    assert order is None
    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any("выключен глобально" in entry.message for entry in entries)


async def test_inactive_strategy_does_nothing(session, setup):
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    assert await auto.execute(session, setup["strategy"], signal) is None


async def test_paper_order_does_not_touch_exchange(session, setup):
    strategy = setup["strategy"]
    await auto.set_active(session, strategy, True)
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    # Адаптер не передаём вовсе: в бумажном режиме он не нужен.
    order = await auto.execute(session, strategy, signal)
    await session.commit()

    assert order is not None
    assert order.mode == MODE_PAPER
    assert order.status == "filled"
    assert order.amount == Decimal("0.0125")
    # Стоп и тейк считаются от цены входа.
    assert order.stop_loss == Decimal(80_000) * Decimal("0.98")
    assert order.take_profit == Decimal(80_000) * Decimal("1.04")


async def test_halted_strategy_skips_with_reason(session, setup):
    strategy = setup["strategy"]
    await auto.set_active(session, strategy, True)

    state = await auto.risk_state(session, strategy)
    state.is_halted = True
    state.halted_reason = "дневной лимит"
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    order = await auto.execute(session, strategy, signal)
    await session.commit()

    assert order is None
    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any("дневной лимит" in entry.message for entry in entries)


async def test_live_mode_without_adapter_refuses(session, setup):
    """Без подключения к бирже реальный ордер не выставляется молча."""
    strategy = setup["strategy"]
    await auto.set_mode(session, setup["user"], strategy, MODE_TESTNET)
    await auto.set_active(session, strategy, True)
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    order = await auto.execute(session, strategy, signal, adapter=None)
    await session.commit()

    assert order is None
    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any("Нет подключения" in entry.message for entry in entries)


async def test_exchange_rejection_is_logged_not_swallowed(session, setup):
    strategy = setup["strategy"]
    await auto.set_mode(session, setup["user"], strategy, MODE_TESTNET)
    await auto.set_active(session, strategy, True)
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    class RefusingAdapter:
        async def create_market_order(self, *args, **kwargs):
            raise RuntimeError("недостаточно средств")

    order = await auto.execute(session, strategy, signal, adapter=RefusingAdapter())
    await session.commit()

    assert order is None
    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any("недостаточно средств" in entry.message for entry in entries)


async def test_order_reaches_exchange_in_testnet(session, setup):
    strategy = setup["strategy"]
    await auto.set_mode(session, setup["user"], strategy, MODE_TESTNET)
    await auto.set_active(session, strategy, True)
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    class RecordingAdapter:
        def __init__(self):
            self.calls = []

        async def create_market_order(self, symbol, side, amount):
            from app.exchanges.base import OrderResult

            self.calls.append((symbol, side, amount))
            return OrderResult(
                external_id="ex-1", symbol=symbol, side=side, amount=amount,
                price=Decimal(80_000), status="closed", filled=amount,
                average_price=Decimal(80_010),
            )

    adapter = RecordingAdapter()
    order = await auto.execute(session, strategy, signal, adapter=adapter)
    await session.commit()

    assert adapter.calls == [("BTC/USDT", "buy", Decimal("0.0125"))]
    assert order.external_id == "ex-1"
    assert order.mode == MODE_TESTNET
    # Цена берётся фактическая, а не расчётная.
    assert order.price == Decimal(80_010)


# --- Лимит убытка ---


async def test_daily_loss_limit_halts_strategy(session, setup):
    strategy = setup["strategy"]
    await auto.set_active(session, strategy, True)
    await session.commit()

    await auto.register_result(session, strategy, Decimal(-3))
    await session.commit()
    assert strategy.is_active, "три процента убытка при лимите пять — работаем"

    await auto.register_result(session, strategy, Decimal(-2.5))
    await session.commit()

    state = await auto.risk_state(session, strategy)
    assert state.is_halted
    assert not strategy.is_active
    assert "лимит" in state.halted_reason

    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any(entry.event_type == auto.EVENT_HALTED for entry in entries)


async def test_profit_does_not_halt(session, setup):
    strategy = setup["strategy"]
    await auto.set_active(session, strategy, True)

    await auto.register_result(session, strategy, Decimal(10))
    await session.commit()

    state = await auto.risk_state(session, strategy)
    assert not state.is_halted
    assert strategy.is_active


async def test_risk_state_is_per_day(session, setup):
    strategy = setup["strategy"]
    state = await auto.risk_state(session, strategy)
    state.realized_pnl_pct = Decimal(-4)
    await session.commit()

    # Тот же день — то же состояние.
    again = await auto.risk_state(session, strategy)
    assert again.id == state.id
    assert again.realized_pnl_pct == Decimal(-4)


# --- Журнал ---


async def test_journal_records_every_decision(session, setup):
    """По ТЗ журнал должен объяснять и действие, и бездействие."""
    strategy = setup["strategy"]
    await auto.set_active(session, strategy, True)
    signal = make_signal(setup)
    session.add(signal)
    await session.commit()

    await auto.execute(session, strategy, signal)
    await session.commit()

    entries = await auto.recent_journal(session, strategy)
    kinds = {entry.event_type for entry in entries}

    assert auto.EVENT_MODE in kinds, "создание и запуск"
    assert auto.EVENT_ORDER in kinds, "сделка"
    assert all(entry.message for entry in entries), "запись без объяснения бесполезна"


async def test_other_users_strategy_is_not_accessible(session, setup):
    stranger = await user_service.create_user(
        session, email="stranger@example.com", password="stranger-password-1"
    )
    await session.commit()

    with pytest.raises(auto.AutotradeError):
        await auto.get_strategy(session, stranger, setup["strategy"].id)
