"""Auto-trading: strategies, execution and risk limits.

The design follows one requirement: a mistake here costs the user money, so by default
nothing happens.

Three layers of protection, each sufficient on its own:
1. The global AUTOTRADE_ENABLED kill switch - off by default.
2. Strategy mode: paper (trades only in the database), testnet (the exchange's test
   network), live (real money). Always starts in paper.
3. Switching to live requires a recorded time of explicit confirmation and an exchange
   key whose trading permission the exchange itself confirmed.

Plus a daily loss limit: when it's reached, the strategy stops itself and logs the
reason.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exchanges.base import ExchangeAdapter
from app.models import (
    Asset,
    BotJournalEntry,
    BotOrder,
    ExchangeAccount,
    Market,
    RiskState,
    Signal,
    SignalRule,
    Strategy,
    User,
)
from app.models.signal import DIRECTION_BUY, DIRECTION_SELL
from app.models.trading import MODE_LIVE, MODE_PAPER, MODE_TESTNET
from app.services import (
    audit_service,
    market_service,
    notification_service,
    portfolio_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()

EVENT_CONSIDERED = "considered"
EVENT_SKIPPED = "skipped"
EVENT_ORDER = "order"
EVENT_CLOSED = "closed"
EVENT_ERROR = "error"
EVENT_HALTED = "halted"
EVENT_MODE = "mode_changed"

MODES = (MODE_PAPER, MODE_TESTNET, MODE_LIVE)

# A bot order describes the whole position: opening writes a row, closing fills
# in its exit price and result. Keeping entry and exit as two rows would mean
# stitching them back together every time.
STATUS_NEW = "new"
STATUS_OPEN = "filled"
STATUS_CLOSED = "closed"

EXIT_STOP_LOSS = "стоп-лосс"
EXIT_TAKE_PROFIT = "тейк-профит"
EXIT_SIGNAL = "обратный сигнал"


class AutotradeError(Exception):
    """The strategy can't be created or started in this form."""


@dataclass(frozen=True)
class Decision:
    """What the strategy decided to do on a signal."""

    action: str  # open | close | skip
    reason: str
    side: str | None = None
    amount: Decimal | None = None


# --- Strategies ---


async def list_strategies(session: AsyncSession, user: User) -> list[tuple[Strategy, str]]:
    result = await session.execute(
        select(Strategy, Market.symbol)
        .join(Market, Market.id == Strategy.market_id)
        .where(Strategy.user_id == user.id)
        .order_by(Strategy.id)
    )
    return [(strategy, symbol) for strategy, symbol in result]


async def get_strategy(session: AsyncSession, user: User, strategy_id: int) -> Strategy:
    result = await session.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user.id)
    )
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise AutotradeError("Стратегия не найдена.")
    return strategy


def validate(
    *,
    position_size_pct: Decimal,
    max_pct_per_trade: Decimal,
    daily_loss_limit_pct: Decimal,
    stop_loss_pct: Decimal | None,
    take_profit_pct: Decimal | None,
) -> None:
    """Validate parameters before saving.

    The bounds are deliberately narrow: a strategy allowed to put the whole deposit into
    one trade isn't a strategy but a way to lose money.
    """
    if not (Decimal("0.1") <= position_size_pct <= Decimal(50)):
        raise AutotradeError("Размер позиции — от 0.1% до 50% депозита.")
    if not (Decimal("0.1") <= max_pct_per_trade <= Decimal(50)):
        raise AutotradeError("Максимум на сделку — от 0.1% до 50% депозита.")
    if position_size_pct > max_pct_per_trade:
        raise AutotradeError("Размер позиции не может превышать максимум на сделку.")
    if not (Decimal("0.5") <= daily_loss_limit_pct <= Decimal(50)):
        raise AutotradeError("Дневной лимит убытка — от 0.5% до 50%.")
    if stop_loss_pct is not None and not (Decimal("0.1") <= stop_loss_pct <= Decimal(90)):
        raise AutotradeError("Стоп-лосс — от 0.1% до 90%.")
    if take_profit_pct is not None and not (Decimal("0.1") <= take_profit_pct <= Decimal(500)):
        raise AutotradeError("Тейк-профит — от 0.1% до 500%.")


async def create_strategy(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    signal_rule_id: int,
    exchange_account_id: int,
    market_id: int,
    position_size_pct: Decimal,
    max_pct_per_trade: Decimal,
    daily_loss_limit_pct: Decimal,
    stop_loss_pct: Decimal | None = None,
    take_profit_pct: Decimal | None = None,
) -> Strategy:
    """Create a strategy. Always disabled and always in paper mode."""
    validate(
        position_size_pct=position_size_pct,
        max_pct_per_trade=max_pct_per_trade,
        daily_loss_limit_pct=daily_loss_limit_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )

    account = await session.get(ExchangeAccount, exchange_account_id)
    if account is None or account.user_id != user.id:
        raise AutotradeError("Подключение биржи не найдено.")

    rule = await session.get(SignalRule, signal_rule_id)
    if rule is None:
        raise AutotradeError("Правило сигналов не найдено.")

    strategy = Strategy(
        user_id=user.id,
        name=name.strip() or "Без названия",
        signal_rule_id=signal_rule_id,
        exchange_account_id=exchange_account_id,
        market_id=market_id,
        mode=MODE_PAPER,
        position_size_pct=position_size_pct,
        max_pct_per_trade=max_pct_per_trade,
        daily_loss_limit_pct=daily_loss_limit_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        is_active=False,
    )
    session.add(strategy)
    await session.flush()

    await journal(session, strategy, EVENT_MODE, "Стратегия создана в режиме бумажных сделок.")
    return strategy


async def set_mode(
    session: AsyncSession, user: User, strategy: Strategy, mode: str
) -> None:
    """Change the mode.

    Switching to live is the only place where access to real money appears, so
    everything is checked here at once.
    """
    if mode not in MODES:
        raise AutotradeError("Неизвестный режим.")

    if mode == MODE_LIVE:
        account = await session.get(ExchangeAccount, strategy.exchange_account_id)
        if account is None or not account.can_trade:
            raise AutotradeError(
                "У ключа нет подтверждённого биржей права на торговлю. "
                "Проверьте разрешения ключа в кабинете биржи."
            )
        if account.is_testnet:
            raise AutotradeError("Ключ от тестовой сети — реальные сделки им невозможны.")

        strategy.live_confirmed_at = datetime.now(timezone.utc)
        await audit_service.log_action(
            session,
            action=audit_service.ACTION_STRATEGY_WENT_LIVE,
            user_id=user.id,
            entity="strategy",
            entity_id=strategy.id,
            payload={"market_id": strategy.market_id},
        )
    else:
        # Leaving live clears the confirmation: switching back will require a
        # new deliberate action.
        strategy.live_confirmed_at = None

    previous, strategy.mode = strategy.mode, mode
    # Changing the mode always stops the strategy: the user must restart it
    # themselves, knowing which mode it's in.
    strategy.is_active = False

    await journal(
        session,
        strategy,
        EVENT_MODE,
        f"Режим изменён с «{previous}» на «{mode}». Стратегия остановлена.",
    )
    await session.flush()


async def set_active(session: AsyncSession, strategy: Strategy, active: bool) -> None:
    if active and strategy.mode == MODE_LIVE and strategy.live_confirmed_at is None:
        raise AutotradeError("Реальные сделки не подтверждены.")

    strategy.is_active = active
    await journal(
        session,
        strategy,
        EVENT_MODE,
        "Стратегия запущена." if active else "Стратегия остановлена.",
    )
    await session.flush()


# --- Risk ---


async def risk_state(session: AsyncSession, strategy: Strategy) -> RiskState:
    """Today's risk state, created afresh if needed."""
    today = datetime.now(timezone.utc).date()
    result = await session.execute(
        select(RiskState).where(
            RiskState.strategy_id == strategy.id,
            RiskState.trading_day == today,
        )
    )
    state = result.scalar_one_or_none()
    if state is None:
        state = RiskState(strategy_id=strategy.id, trading_day=today)
        session.add(state)
        await session.flush()
    return state


async def register_result(
    session: AsyncSession, strategy: Strategy, pnl_pct: Decimal
) -> RiskState:
    """Account for a trade result and stop the strategy if the limit is exceeded.

    The limit is checked after every trade, not once a day: the point of the limit is to
    stop before the loss grows.
    """
    state = await risk_state(session, strategy)
    state.realized_pnl_pct += pnl_pct
    state.trades_count += 1

    if state.realized_pnl_pct <= -abs(strategy.daily_loss_limit_pct):
        state.is_halted = True
        state.halted_reason = (
            f"Дневной убыток {state.realized_pnl_pct:.2f}% достиг лимита "
            f"{strategy.daily_loss_limit_pct}%."
        )
        state.halted_at = datetime.now(timezone.utc)
        strategy.is_active = False
        await journal(session, strategy, EVENT_HALTED, state.halted_reason)
        # The bot stopped itself - that must not happen silently: the person
        # should find out some other way than from the log on their next visit.
        await notification_service.dispatch(
            session,
            user_id=strategy.user_id,
            kind=notification_service.KIND_SYSTEM,
            title=f"Стратегия «{strategy.name}» остановлена",
            body=state.halted_reason,
            payload={"strategy_id": strategy.id},
        )

    await session.flush()
    return state


# --- Execution ---


def decide(
    *,
    strategy: Strategy,
    signal: Signal,
    equity_usd: Decimal,
    price: Decimal,
    has_open_position: bool,
    min_amount: Decimal | None = None,
) -> Decision:
    """What to do on a signal. Pure function - covered by a test.

    The strategy trades spot and holds at most one position: a buy opens it, a sell
    closes it. Selling with no open position isn't a "short" but selling the user's own
    coins, so it is rejected.

    Position size is computed from the deposit valuation, not the free balance:
    otherwise the size quietly drifts after a series of trades.
    """
    if price <= 0:
        return Decision(action="skip", reason="Нет текущей цены.")

    if signal.direction == DIRECTION_SELL:
        if not has_open_position:
            return Decision(
                action="skip",
                reason="Продавать нечего: открытой позиции нет, а шорт на споте невозможен.",
            )
        return Decision(action="close", reason=f"Закрытие по сигналу: {signal.reason}")

    if signal.direction != DIRECTION_BUY:
        return Decision(action="skip", reason="Сигнал без направления.")

    if has_open_position:
        return Decision(
            action="skip",
            reason="Позиция уже открыта — вторую по тому же сигналу не набираем.",
        )
    if equity_usd <= 0:
        return Decision(action="skip", reason="Оценка депозита нулевая.")

    share = min(strategy.position_size_pct, strategy.max_pct_per_trade)
    notional = equity_usd * share / Decimal(100)
    amount = notional / price

    if amount <= 0:
        return Decision(action="skip", reason="Расчётный объём нулевой.")

    # The minimum lot is checked in all modes, including paper: a trade below
    # the exchange minimum wouldn't have happened, and recording it in the
    # paper result would promise profit that won't materialize.
    if min_amount is not None and amount < min_amount:
        return Decision(
            action="skip",
            reason=(
                f"Расчётный объём {_num(amount)} меньше минимального "
                f"{_num(min_amount)} для этой пары — увеличьте размер позиции."
            ),
        )

    return Decision(
        action="open",
        reason=(
            f"Сигнал {signal.direction}: {_num(share)}% депозита "
            f"({notional.quantize(Decimal('0.01'))} USD) по цене {_num(price)}."
        ),
        side=signal.direction,
        amount=amount,
    )


def exit_reason(order: BotOrder, price: Decimal) -> str | None:
    """Whether an exit level has been reached. Pure function.

    The stop is checked before the take: if the price touched both levels within one
    interval, we don't know the order of events and must assume the worse. The opposite
    assumption would make the strategy's reports nicer than reality.
    """
    if price is None or price <= 0:
        return None
    if order.stop_loss is not None and price <= order.stop_loss:
        return EXIT_STOP_LOSS
    if order.take_profit is not None and price >= order.take_profit:
        return EXIT_TAKE_PROFIT
    return None


def position_pnl(order: BotOrder, exit_price: Decimal) -> tuple[Decimal, Decimal]:
    """Position result: in dollars and as a percentage of the amount invested."""
    entry = order.price or Decimal(0)
    if entry <= 0:
        return Decimal(0), Decimal(0)

    pnl_usd = (exit_price - entry) * order.amount
    pnl_pct = (exit_price - entry) / entry * Decimal(100)
    return pnl_usd, pnl_pct


def mark_considered(strategy: Strategy, signal: Signal) -> None:
    """Remember that this signal has already been processed.

    The marker only grows: signals arrive in increasing id order, and moving back would
    mean re-processing old ones.
    """
    if strategy.last_signal_id is None or signal.id > strategy.last_signal_id:
        strategy.last_signal_id = signal.id


async def open_position(session: AsyncSession, strategy: Strategy) -> BotOrder | None:
    """The strategy's open position, if any."""
    result = await session.execute(
        select(BotOrder)
        .where(
            BotOrder.strategy_id == strategy.id,
            BotOrder.closed_at.is_(None),
            BotOrder.status != STATUS_CLOSED,
        )
        .order_by(BotOrder.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def execute(
    session: AsyncSession,
    strategy: Strategy,
    signal: Signal,
    *,
    adapter: ExchangeAdapter | None = None,
) -> BotOrder | None:
    """Execute the decision for a signal.

    In paper mode the exchange isn't touched at all: the order is written to the
    database at the price of the latest quote. In testnet and live a market order is
    sent.
    """
    if not settings.autotrade_enabled:
        await journal(
            session, strategy, EVENT_SKIPPED,
            "Автотрейдинг выключен глобально (AUTOTRADE_ENABLED).",
        )
        return None

    if not strategy.is_active:
        return None

    # From here on every branch logs something about this signal, and it must
    # be reviewed exactly once: otherwise the next pass repeats the same entry,
    # and so on for half an hour while the signal is fresh.
    mark_considered(strategy, signal)

    state = await risk_state(session, strategy)
    if state.is_halted:
        await journal(
            session, strategy, EVENT_SKIPPED,
            f"Стратегия остановлена: {state.halted_reason}",
        )
        return None

    ticker = await market_service.get_ticker(session, strategy.market_id)
    price = ticker.last if ticker else None
    if price is None:
        await journal(session, strategy, EVENT_SKIPPED, "Нет текущей котировки.")
        return None

    user = await session.get(User, strategy.user_id)
    summary = await portfolio_service.build_summary(session, user)
    position = await open_position(session, strategy)
    market = await session.get(Market, strategy.market_id)

    decision = decide(
        strategy=strategy,
        signal=signal,
        equity_usd=summary.total_usd,
        price=price,
        has_open_position=position is not None,
        min_amount=market.min_amount if market else None,
    )

    if decision.action == "close":
        return await close_position(
            session, strategy, position, price, decision.reason, adapter=adapter
        )

    if decision.action != "open":
        await journal(session, strategy, EVENT_SKIPPED, decision.reason)
        return None

    order = BotOrder(
        strategy_id=strategy.id,
        signal_id=signal.id,
        exchange_account_id=strategy.exchange_account_id,
        market_id=strategy.market_id,
        mode=strategy.mode,
        side=decision.side,
        amount=decision.amount,
        price=price,
        status=STATUS_OPEN if strategy.mode == MODE_PAPER else STATUS_NEW,
        stop_loss=_level(price, strategy.stop_loss_pct, decision.side, stop=True),
        take_profit=_level(price, strategy.take_profit_pct, decision.side, stop=False),
    )

    if strategy.mode == MODE_PAPER:
        session.add(order)
        await journal(
            session, strategy, EVENT_ORDER,
            f"Бумажная сделка: {decision.reason}",
            payload={"amount": str(decision.amount), "price": str(price)},
        )
        await session.flush()
        return order

    if adapter is None:
        await journal(
            session, strategy, EVENT_ERROR,
            "Нет подключения к бирже — ордер не выставлен.",
        )
        return None

    try:
        result = await adapter.create_market_order(market.symbol, decision.side, decision.amount)
    except Exception as exc:
        await journal(session, strategy, EVENT_ERROR, f"Биржа отклонила ордер: {exc}")
        await session.flush()
        return None

    order.external_order_id = result.external_id
    order.status = result.status
    order.price = result.average_price or price
    # The exchange rounds the amount to its lot step: the database must keep
    # what it accepted, otherwise the result calculation drifts from reality.
    order.amount = result.amount
    order.raw = result.raw or None
    session.add(order)

    await journal(
        session, strategy, EVENT_ORDER,
        f"Ордер на бирже ({strategy.mode}): {decision.reason}",
        payload={"external_id": result.external_id, "status": result.status},
    )
    await session.flush()
    return order


async def check_exits(
    session: AsyncSession,
    strategy: Strategy,
    *,
    adapter: ExchangeAdapter | None = None,
) -> BotOrder | None:
    """Close the position if the price reached the stop or the take.

    Called on every pass, not only on a new signal: exit levels are meant to fire on
    their own. Without this check a stop-loss would be a number in the database, not
    protection.
    """
    position = await open_position(session, strategy)
    if position is None:
        return None

    ticker = await market_service.get_ticker(session, strategy.market_id)
    price = ticker.last if ticker else None
    if price is None:
        return None

    reason = exit_reason(position, price)
    if reason is None:
        return None

    return await close_position(
        session, strategy, position, price, f"Сработал {reason}.", adapter=adapter
    )


async def close_position(
    session: AsyncSession,
    strategy: Strategy,
    order: BotOrder,
    price: Decimal,
    reason: str,
    *,
    adapter: ExchangeAdapter | None = None,
) -> BotOrder | None:
    """Close the position and account for the result in the daily limit.

    The result is computed as a percentage of the deposit, not of the position itself:
    the daily loss limit in the spec is a share of the deposit, and a 2% stop on a
    position worth 5% of the deposit costs 0.1%, not 2%. Mixing these up would stop the
    bot twenty times too early.
    """
    if order is None:
        return None

    if strategy.mode != MODE_PAPER:
        if adapter is None:
            await journal(
                session, strategy, EVENT_ERROR,
                "Нет подключения к бирже — позиция не закрыта.",
            )
            return None

        market = await session.get(Market, strategy.market_id)
        base = await session.get(Asset, market.base_asset_id) if market else None

        amount = await _sellable_amount(session, strategy, adapter, base, order.amount)
        if amount is None:
            return None

        opposite = DIRECTION_SELL if order.side == DIRECTION_BUY else DIRECTION_BUY
        try:
            result = await adapter.create_market_order(market.symbol, opposite, amount)
        except Exception as exc:
            await journal(session, strategy, EVENT_ERROR, f"Биржа отклонила закрытие: {exc}")
            await session.flush()
            return None
        price = result.average_price or price

    pnl_usd, pnl_pct = position_pnl(order, price)

    order.close_price = price
    order.realized_pnl = pnl_usd
    order.closed_at = datetime.now(timezone.utc)
    order.status = STATUS_CLOSED

    user = await session.get(User, strategy.user_id)
    summary = await portfolio_service.build_summary(session, user)
    equity = summary.total_usd
    pnl_of_equity = (pnl_usd / equity * Decimal(100)) if equity > 0 else Decimal(0)

    await journal(
        session, strategy, EVENT_CLOSED,
        f"{reason} Выход по {_num(price)}, результат "
        f"{pnl_usd.quantize(Decimal('0.01'))} USD "
        f"({pnl_pct.quantize(Decimal('0.01'))}% позиции).",
        payload={
            "order_id": order.id,
            "exit_price": str(price),
            "pnl_usd": str(pnl_usd),
            "pnl_pct": str(pnl_pct),
        },
    )

    await register_result(session, strategy, pnl_of_equity)
    await session.flush()
    return order


async def _sellable_amount(
    session: AsyncSession,
    strategy: Strategy,
    adapter: ExchangeAdapter,
    base: "Asset | None",
    wanted: Decimal,
) -> Decimal | None:
    """How much of the coin can actually be sold when closing.

    Selling exactly what was bought usually isn't possible: the exchange often takes the
    fee in the coin itself, so the balance ends up slightly smaller than the order. An
    order for the full amount returns "insufficient funds", and the position stays open -
    with its stop-loss gone and nobody aware of it.

    So the amount is capped at the free balance. None means there's nothing to close
    with and the reason has already been logged.
    """
    if base is None:
        return wanted

    try:
        balances = await adapter.fetch_balances()
    except Exception as exc:
        # Couldn't get the balance - try closing with the full amount: an
        # exchange rejection will be logged in the next step.
        logger.warning("Could not get balance before closing: %s", exc)
        return wanted

    free = next(
        (entry.free for entry in balances if entry.asset == base.symbol), Decimal(0)
    )
    if free <= 0:
        await journal(
            session, strategy, EVENT_ERROR,
            f"На балансе нет {base.symbol} — закрывать нечем. "
            "Проверьте, не продана ли позиция вручную.",
        )
        await session.flush()
        return None

    if free >= wanted:
        return wanted

    await journal(
        session, strategy, EVENT_CLOSED,
        f"Свободно {_num(free)} {base.symbol} вместо {_num(wanted)} — "
        "закрываем остатком (комиссия биржи удержана монетой).",
    )
    return free


async def journal(
    session: AsyncSession,
    strategy: Strategy,
    event_type: str,
    message: str,
    *,
    payload: dict | None = None,
) -> BotJournalEntry:
    """Record a bot action.

    Per the spec the log is complete: placed orders, rejections and errors alike. The
    user must be able to see why the bot did or didn't do what they expected.
    """
    entry = BotJournalEntry(
        strategy_id=strategy.id,
        event_type=event_type,
        message=message,
        payload=payload,
    )
    session.add(entry)
    await session.flush()
    return entry


async def recent_journal(
    session: AsyncSession, strategy: Strategy, *, limit: int = 100
) -> list[BotJournalEntry]:
    result = await session.execute(
        select(BotJournalEntry)
        .where(BotJournalEntry.strategy_id == strategy.id)
        .order_by(BotJournalEntry.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def recent_orders(
    session: AsyncSession, strategy: Strategy, *, limit: int = 50
) -> list[BotOrder]:
    result = await session.execute(
        select(BotOrder)
        .where(BotOrder.strategy_id == strategy.id)
        .order_by(BotOrder.opened_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


def _num(value: Decimal | None) -> str:
    """A number in a log message without trailing zeros.

    Numeric(36, 18) returns 79903.000000000000000000, and in the log that reads as a
    glitch, not a price.
    """
    if value is None:
        return "—"
    return format(value.normalize(), "f")


def _level(
    price: Decimal, pct: Decimal | None, side: str, *, stop: bool
) -> Decimal | None:
    """Stop-loss or take-profit price from the entry price."""
    if pct is None:
        return None

    share = abs(pct) / Decimal(100)
    if side == DIRECTION_BUY:
        return price * (1 - share) if stop else price * (1 + share)
    return price * (1 + share) if stop else price * (1 - share)
