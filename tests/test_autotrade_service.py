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
        has_open_position=False,
    )

    assert decision.action == "open"
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
        has_open_position=False,
    )

    assert decision.amount == Decimal(5), "5% от 10 000 при цене 100"


def test_no_order_without_equity_or_price():
    strategy = type("S", (), {
        "position_size_pct": Decimal(10), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "buy"})()

    assert auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(0), price=Decimal(100),
        has_open_position=False,
    ).action == "skip"
    assert auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(100), price=Decimal(0),
        has_open_position=False,
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
    # Именно колонка, а не одноимённый атрибут: присваивание чужого имени
    # ORM молча проглатывает, и id ордера с биржи не сохранялся.
    assert order.external_order_id == "ex-1"
    reloaded = await session.get(BotOrder, order.id)
    assert reloaded.external_order_id == "ex-1"
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


# --- Жизненный цикл позиции ---


def make_order(price: str, amount: str, *, stop: str | None = None, take: str | None = None):
    return type("O", (), {
        "price": Decimal(price),
        "amount": Decimal(amount),
        "stop_loss": Decimal(stop) if stop else None,
        "take_profit": Decimal(take) if take else None,
        "side": "buy",
    })()


def test_stop_loss_wins_when_both_levels_touched():
    """Порядок событий внутри интервала неизвестен — считаем по худшему."""
    order = make_order("100", "1", stop="98", take="104")

    assert auto.exit_reason(order, Decimal(97)) == auto.EXIT_STOP_LOSS
    assert auto.exit_reason(order, Decimal(105)) == auto.EXIT_TAKE_PROFIT
    assert auto.exit_reason(order, Decimal(100)) is None


def test_exit_ignores_missing_levels():
    order = make_order("100", "1")
    assert auto.exit_reason(order, Decimal(1)) is None
    assert auto.exit_reason(order, Decimal(10_000)) is None


def test_position_pnl_counts_both_ways():
    order = make_order("100", "2")

    usd, pct = auto.position_pnl(order, Decimal(110))
    assert usd == Decimal(20)
    assert pct == Decimal(10)

    usd, pct = auto.position_pnl(order, Decimal(90))
    assert usd == Decimal(-20)
    assert pct == Decimal(-10)


def test_second_buy_does_not_stack_position():
    strategy = type("S", (), {
        "position_size_pct": Decimal(10), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "buy", "reason": "проверка"})()

    decision = auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(10_000),
        price=Decimal(100), has_open_position=True,
    )
    assert decision.action == "skip"


def test_sell_without_position_is_refused():
    """На споте это была бы продажа монет самого пользователя."""
    strategy = type("S", (), {
        "position_size_pct": Decimal(10), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "sell", "reason": "проверка"})()

    decision = auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(10_000),
        price=Decimal(100), has_open_position=False,
    )
    assert decision.action == "skip"


def test_sell_with_position_closes_it():
    strategy = type("S", (), {
        "position_size_pct": Decimal(10), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "sell", "reason": "пересечение вниз"})()

    decision = auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(10_000),
        price=Decimal(100), has_open_position=True,
    )
    assert decision.action == "close"


async def test_paper_position_opens_once_and_closes_by_signal(session, setup):
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    buy = make_signal(setup)
    session.add(buy)
    await session.flush()

    opened = await auto.execute(session, strategy, buy)
    assert opened is not None
    assert opened.status == auto.STATUS_OPEN

    # Второй сигнал на покупку не должен набирать позицию заново.
    another_buy = make_signal(setup)
    session.add(another_buy)
    await session.flush()
    assert await auto.execute(session, strategy, another_buy) is None

    ticker = await session.get(MarketTicker, setup["market"].id)
    ticker.last = Decimal(88_000)
    sell = make_signal(setup, direction="sell")
    session.add(sell)
    await session.flush()

    closed = await auto.execute(session, strategy, sell)
    await session.commit()

    assert closed is not None
    assert closed.id == opened.id, "закрывается та же позиция, а не заводится новая"
    assert closed.status == auto.STATUS_CLOSED
    assert closed.close_price == Decimal(88_000)
    assert closed.closed_at is not None
    assert closed.realized_pnl > 0


async def test_stop_loss_closes_position_without_signal(session, setup):
    """Стоп-лосс на то и стоп, что срабатывает сам."""
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    signal = make_signal(setup)
    session.add(signal)
    await session.flush()
    opened = await auto.execute(session, strategy, signal)
    assert opened.stop_loss == Decimal(78_400), "стоп 2% от 80 000"

    ticker = await session.get(MarketTicker, setup["market"].id)
    ticker.last = Decimal(78_000)
    await session.flush()

    closed = await auto.check_exits(session, strategy)
    await session.commit()

    assert closed is not None
    assert closed.realized_pnl < 0
    assert await auto.open_position(session, strategy) is None

    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any(auto.EXIT_STOP_LOSS in entry.message for entry in entries)


async def test_take_profit_closes_position(session, setup):
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    signal = make_signal(setup)
    session.add(signal)
    await session.flush()
    opened = await auto.execute(session, strategy, signal)
    assert opened.take_profit == Decimal(83_200), "тейк 4% от 80 000"

    ticker = await session.get(MarketTicker, setup["market"].id)
    ticker.last = Decimal(84_000)
    await session.flush()

    closed = await auto.check_exits(session, strategy)
    await session.commit()

    assert closed is not None
    assert closed.realized_pnl > 0


async def test_check_exits_quiet_while_price_between_levels(session, setup):
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    signal = make_signal(setup)
    session.add(signal)
    await session.flush()
    await auto.execute(session, strategy, signal)

    assert await auto.check_exits(session, strategy) is None
    assert await auto.open_position(session, strategy) is not None


async def test_daily_limit_counts_share_of_deposit_not_of_position(session, setup):
    """Стоп в 2% на позиции в 10% депозита стоит 0.2%, а не 2%.

    Перепутать эти величины значило бы останавливать бота в разы раньше
    срока — и дневной лимит убытка перестал бы что-либо значить.
    """
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    signal = make_signal(setup)
    session.add(signal)
    await session.flush()
    await auto.execute(session, strategy, signal)

    ticker = await session.get(MarketTicker, setup["market"].id)
    ticker.last = Decimal(78_400)
    await session.flush()

    await auto.check_exits(session, strategy)
    await session.commit()

    state = await auto.risk_state(session, strategy)
    assert state.trades_count == 1
    # Позиция — 10% депозита, стоп −2% от неё: около −0.2% депозита.
    assert Decimal("-0.5") < state.realized_pnl_pct < Decimal(0)
    assert state.is_halted is False


async def test_daily_limit_halts_after_enough_losses(session, setup):
    """Лимит должен действительно останавливать, а не просто считаться."""
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    await auto.register_result(session, strategy, Decimal(-6))
    await session.commit()

    state = await auto.risk_state(session, strategy)
    assert state.is_halted is True
    assert strategy.is_active is False


def test_journal_numbers_have_no_zero_tail():
    """Numeric(36, 18) в тексте журнала читается как сбой, а не как цена."""
    strategy = type("S", (), {
        "position_size_pct": Decimal("5.0000"), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "buy", "reason": "проверка"})()

    decision = auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(10_000),
        price=Decimal("79903.000000000000000000"), has_open_position=False,
    )

    # Точка в конце предложения — не хвост: проверяем именно нули.
    assert decision.reason.endswith("по цене 79903.")
    assert "79903.0" not in decision.reason
    assert "5%" in decision.reason and "5.0000%" not in decision.reason


# --- Живой путь: объём, минимальный лот и остаток на балансе ---


def test_amount_below_exchange_minimum_is_refused():
    """Такая сделка не состоялась бы и на бирже — значит, и в бумажной.

    Записать её в бумажный результат значило бы обещать прибыль, которой
    не будет.
    """
    strategy = type("S", (), {
        "position_size_pct": Decimal("0.1"), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "buy", "reason": "проверка"})()

    decision = auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(100),
        price=Decimal(80_000), has_open_position=False,
        min_amount=Decimal("0.0001"),
    )

    assert decision.action == "skip"
    assert "минимального" in decision.reason


def test_amount_above_minimum_passes():
    strategy = type("S", (), {
        "position_size_pct": Decimal(10), "max_pct_per_trade": Decimal(20),
    })()
    signal = type("Sig", (), {"direction": "buy", "reason": "проверка"})()

    decision = auto.decide(
        strategy=strategy, signal=signal, equity_usd=Decimal(10_000),
        price=Decimal(80_000), has_open_position=False,
        min_amount=Decimal("0.0001"),
    )

    assert decision.action == "open"


class ClosingAdapter:
    """Биржа, у которой на балансе меньше, чем было куплено."""

    def __init__(self, free: str | None):
        self.free = Decimal(free) if free is not None else None
        self.orders: list[tuple] = []

    async def fetch_balances(self):
        if self.free is None:
            raise RuntimeError("баланс недоступен")
        return [fakes.balance("BTC", str(self.free))]

    async def create_market_order(self, symbol, side, amount):
        from app.exchanges.base import OrderResult

        self.orders.append((symbol, side, amount))
        return OrderResult(
            external_id="close-1", symbol=symbol, side=side, amount=amount,
            price=Decimal(88_000), status="closed", filled=amount,
            average_price=Decimal(88_000),
        )


async def test_close_sells_only_what_is_on_balance(session, setup):
    """Комиссию биржа удерживает монетой — купленный объём продать нельзя."""
    strategy = setup["strategy"]
    strategy.is_active = True
    strategy.mode = MODE_TESTNET
    await session.flush()

    signal = make_signal(setup)
    session.add(signal)
    await session.flush()

    opening = ClosingAdapter(free="1")
    order = await auto.execute(session, strategy, signal, adapter=opening)
    bought = order.amount

    closing = ClosingAdapter(free=str(bought * Decimal("0.999")))
    closed = await auto.close_position(
        session, strategy, order, Decimal(88_000), "проверка", adapter=closing
    )
    await session.commit()

    assert closed is not None
    _, side, sold = closing.orders[0]
    assert side == "sell"
    assert sold < bought, "продаём остаток, а не полный объём"

    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any("комиссия биржи" in entry.message for entry in entries)


async def test_close_refuses_when_balance_is_empty(session, setup):
    """Позицию продали вручную — молчать об этом нельзя."""
    strategy = setup["strategy"]
    strategy.is_active = True
    strategy.mode = MODE_TESTNET
    await session.flush()

    signal = make_signal(setup)
    session.add(signal)
    await session.flush()
    order = await auto.execute(session, strategy, signal, adapter=ClosingAdapter(free="1"))

    empty = ClosingAdapter(free="0")
    assert await auto.close_position(
        session, strategy, order, Decimal(88_000), "проверка", adapter=empty
    ) is None
    await session.commit()

    assert empty.orders == [], "заявку на пустой баланс отправлять незачем"
    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    assert any("закрывать нечем" in entry.message for entry in entries)


async def test_close_falls_back_to_full_amount_when_balance_unknown(session, setup):
    """Не смогли узнать остаток — пробуем закрыть, отказ попадёт в журнал."""
    strategy = setup["strategy"]
    strategy.is_active = True
    strategy.mode = MODE_TESTNET
    await session.flush()

    signal = make_signal(setup)
    session.add(signal)
    await session.flush()
    order = await auto.execute(session, strategy, signal, adapter=ClosingAdapter(free="1"))

    blind = ClosingAdapter(free=None)
    closed = await auto.close_position(
        session, strategy, order, Decimal(88_000), "проверка", adapter=blind
    )
    await session.commit()

    assert closed is not None
    assert blind.orders[0][2] == order.amount


async def test_signal_is_considered_once(session, setup):
    """Иначе каждый проход пишет в журнал тот же отказ.

    Сигнал остаётся свежим полчаса, фоновый процесс ходит раз в минуту —
    получалось три десятка одинаковых строк, и единственная важная
    терялась среди них.
    """
    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    signal = make_signal(setup, direction="sell")  # отказ гарантирован
    session.add(signal)
    await session.flush()

    for _ in range(3):
        await auto.execute(session, strategy, signal)
    await session.commit()

    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    refusals = [e for e in entries if e.event_type == auto.EVENT_SKIPPED]
    assert len(refusals) == 3, "сам по себе execute не дедуплицирует"

    # А отметка о разборе выставлена — по ней выборка и отсекает повтор.
    assert strategy.last_signal_id == signal.id


async def test_watermark_only_moves_forward(session, setup):
    """Откат назад заставил бы разбирать старое заново."""
    strategy = setup["strategy"]
    strategy.last_signal_id = 100

    auto.mark_considered(strategy, type("S", (), {"id": 50})())
    assert strategy.last_signal_id == 100

    auto.mark_considered(strategy, type("S", (), {"id": 150})())
    assert strategy.last_signal_id == 150


async def test_worker_does_not_reconsider_the_same_signal(session, setup, monkeypatch):
    """Проверка самого исправления: повторного разбора быть не должно."""
    from contextlib import asynccontextmanager

    from app.worker import tasks

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks.get_settings(), "autotrade_enabled", True, raising=False)

    strategy = setup["strategy"]
    strategy.is_active = True
    await session.flush()

    signal = make_signal(setup, direction="sell")
    session.add(signal)
    await session.commit()

    for _ in range(3):
        await tasks._run_strategy(strategy.id)

    entries = (await session.execute(select(BotJournalEntry))).scalars().all()
    refusals = [e for e in entries if e.event_type == auto.EVENT_SKIPPED]

    assert len(refusals) == 1, "три прохода фонового процесса — одна запись"
