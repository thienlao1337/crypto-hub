"""Автотрейдинг: стратегии, исполнение и лимиты риска.

Устройство подчинено одному требованию: ошибка здесь стоит денег
пользователя, поэтому по умолчанию не происходит ничего.

Три уровня защиты, каждый из которых достаточен сам по себе:
1. Глобальный рубильник AUTOTRADE_ENABLED — выключен по умолчанию.
2. Режим стратегии: paper (сделки только в базе), testnet (тестовая сеть
   биржи), live (реальные деньги). Стартует всегда с paper.
3. Переход в live требует записанного времени явного подтверждения и
   ключа биржи, которому сама биржа подтвердила право на торговлю.

Плюс дневной лимит убытка: при его достижении стратегия останавливает
себя сама и пишет причину в журнал.
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
from app.services import audit_service, market_service, portfolio_service

logger = logging.getLogger(__name__)
settings = get_settings()

EVENT_CONSIDERED = "considered"
EVENT_SKIPPED = "skipped"
EVENT_ORDER = "order"
EVENT_ERROR = "error"
EVENT_HALTED = "halted"
EVENT_MODE = "mode_changed"

MODES = (MODE_PAPER, MODE_TESTNET, MODE_LIVE)


class AutotradeError(Exception):
    """Стратегию нельзя создать или запустить в таком виде."""


@dataclass(frozen=True)
class Decision:
    """Что стратегия решила сделать по сигналу."""

    action: str  # order | skip
    reason: str
    side: str | None = None
    amount: Decimal | None = None


# --- Стратегии ---


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
    """Проверить параметры до сохранения.

    Границы намеренно узкие: стратегия, которой разрешено ставить весь
    депозит в одну сделку, — не стратегия, а способ потерять деньги.
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
    """Создать стратегию. Всегда выключенной и всегда в режиме paper."""
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
    """Сменить режим.

    Переход в live — единственное место, где появляется доступ к
    реальным деньгам, поэтому здесь проверяется всё сразу.
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
        # Выход из live гасит подтверждение: следующий переход туда
        # потребует нового осознанного действия.
        strategy.live_confirmed_at = None

    previous, strategy.mode = strategy.mode, mode
    # Смена режима всегда останавливает стратегию: запускать её обратно
    # пользователь должен сам, уже понимая, в каком она режиме.
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


# --- Риск ---


async def risk_state(session: AsyncSession, strategy: Strategy) -> RiskState:
    """Состояние риска за сегодня, при необходимости заводится заново."""
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
    """Учесть результат сделки и остановить стратегию при переборе.

    Лимит проверяется после каждой сделки, а не раз в день: смысл
    ограничения в том, чтобы остановиться до того, как убыток вырастет.
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

    await session.flush()
    return state


# --- Исполнение ---


def decide(
    *,
    strategy: Strategy,
    signal: Signal,
    equity_usd: Decimal,
    price: Decimal,
) -> Decision:
    """Что делать по сигналу. Чистая функция — её проверяет тест.

    Размер позиции считается от оценки депозита, а не от свободного
    остатка: иначе после серии сделок объём незаметно уплывает.
    """
    if signal.direction not in (DIRECTION_BUY, DIRECTION_SELL):
        return Decision(action="skip", reason="Сигнал без направления.")
    if price <= 0:
        return Decision(action="skip", reason="Нет текущей цены.")
    if equity_usd <= 0:
        return Decision(action="skip", reason="Оценка депозита нулевая.")

    share = min(strategy.position_size_pct, strategy.max_pct_per_trade)
    notional = equity_usd * share / Decimal(100)
    amount = notional / price

    if amount <= 0:
        return Decision(action="skip", reason="Расчётный объём нулевой.")

    return Decision(
        action="order",
        reason=(
            f"Сигнал {signal.direction}: {share}% депозита "
            f"({notional.quantize(Decimal('0.01'))} USD) по цене {price}."
        ),
        side=signal.direction,
        amount=amount,
    )


async def execute(
    session: AsyncSession,
    strategy: Strategy,
    signal: Signal,
    *,
    adapter: ExchangeAdapter | None = None,
) -> BotOrder | None:
    """Исполнить решение по сигналу.

    В режиме paper биржа не дёргается вовсе: ордер записывается в базу с
    ценой из последней котировки. В testnet и live уходит рыночный ордер.
    """
    if not settings.autotrade_enabled:
        await journal(
            session, strategy, EVENT_SKIPPED,
            "Автотрейдинг выключен глобально (AUTOTRADE_ENABLED).",
        )
        return None

    if not strategy.is_active:
        return None

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

    decision = decide(
        strategy=strategy, signal=signal, equity_usd=summary.total_usd, price=price
    )
    if decision.action != "order":
        await journal(session, strategy, EVENT_SKIPPED, decision.reason)
        return None

    market = await session.get(Market, strategy.market_id)
    order = BotOrder(
        strategy_id=strategy.id,
        signal_id=signal.id,
        exchange_account_id=strategy.exchange_account_id,
        market_id=strategy.market_id,
        mode=strategy.mode,
        side=decision.side,
        amount=decision.amount,
        price=price,
        status="filled" if strategy.mode == MODE_PAPER else "new",
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

    order.external_id = result.external_id
    order.status = result.status
    order.price = result.average_price or price
    order.raw = result.raw or None
    session.add(order)

    await journal(
        session, strategy, EVENT_ORDER,
        f"Ордер на бирже ({strategy.mode}): {decision.reason}",
        payload={"external_id": result.external_id, "status": result.status},
    )
    await session.flush()
    return order


async def journal(
    session: AsyncSession,
    strategy: Strategy,
    event_type: str,
    message: str,
    *,
    payload: dict | None = None,
) -> BotJournalEntry:
    """Записать действие бота.

    По ТЗ журнал ведётся полностью: и выставленный ордер, и отказ, и
    ошибка. Пользователь должен видеть, почему бот сделал или не сделал
    то, чего он ждал.
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


def _level(
    price: Decimal, pct: Decimal | None, side: str, *, stop: bool
) -> Decimal | None:
    """Цена стоп-лосса или тейк-профита от цены входа."""
    if pct is None:
        return None

    share = abs(pct) / Decimal(100)
    if side == DIRECTION_BUY:
        return price * (1 - share) if stop else price * (1 + share)
    return price * (1 + share) if stop else price * (1 - share)
