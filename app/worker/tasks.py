"""Фоновые задачи.

Правило, общее для всех задач ниже: единица работы (одно подключение,
одна биржа, один пользователь) обрабатывается в своей сессии.

Так сделано не из аккуратности, а по необходимости. Откат помечает все
загруженные ORM-объекты протухшими, и следующее обращение к любому их
полю — даже к id в строке журнала — тянет SELECT из синхронного кода,
что в асинхронном SQLAlchemy падает с MissingGreenlet. При общей сессии
сбой на первом подключении ронял бы обработку всех остальных. Поэтому
списки собираются простыми значениями заранее, а объекты загружаются уже
внутри своей транзакции.
"""

import logging

from sqlalchemy import select

from app.db import session_scope
from app.exchanges.ccxt_client import CcxtAdapter
from app.models import (
    Alert,
    Exchange,
    ExchangeAccount,
    Market,
    SignalRule,
    Timeframe,
    User,
    WatchlistItem,
)
from app.models.exchange import KEY_STATUS_INVALID
from app.services import exchange_keys_service as keys_service
from app.services import (
    alert_service,
    candle_service,
    market_service,
    portfolio_service,
    signal_service,
)

logger = logging.getLogger(__name__)

# Пары нужны в первую очередь к стейблкоинам: по ним считается оценка
# портфеля. Остальные подтянутся, когда дойдёт дело до графиков.
QUOTE_FILTER = {"USDT", "USDC"}


async def refresh_markets() -> None:
    """Обновить справочник торговых пар. Достаточно раз в сутки."""
    for code in await _active_exchange_codes():
        try:
            async with session_scope() as session:
                exchange = await _exchange_by_code(session, code)
                async with CcxtAdapter(code) as adapter:
                    count = await market_service.sync_markets(
                        session, exchange, adapter, only_quotes=QUOTE_FILTER
                    )
                await session.commit()
            logger.info("Пары %s обновлены: %s", code, count)
        except Exception:
            logger.exception("Не удалось обновить пары %s", code)


async def refresh_tickers() -> None:
    """Обновить срез котировок. На нём держится оценка портфеля."""
    for code in await _active_exchange_codes():
        try:
            async with session_scope() as session:
                exchange = await _exchange_by_code(session, code)
                async with CcxtAdapter(code) as adapter:
                    count = await market_service.update_tickers(session, exchange, adapter)
                await session.commit()
            logger.debug("Котировки %s обновлены: %s", code, count)
        except Exception:
            logger.exception("Не удалось обновить котировки %s", code)


async def sync_balances() -> None:
    """Обновить балансы по всем действующим подключениям."""
    for account_id in await _syncable_account_ids():
        try:
            async with session_scope() as session:
                account = await session.get(ExchangeAccount, account_id)
                if account is None:
                    continue

                adapter = await keys_service.build_adapter(session, account)
                try:
                    count = await portfolio_service.sync_balances(session, account, adapter)
                    await keys_service.mark_synced(session, account)
                    await session.commit()
                finally:
                    await adapter.close()
            logger.debug("Балансы подключения %s: %s монет", account_id, count)
        except Exception as exc:
            logger.warning("Балансы подключения %s не обновлены: %s", account_id, exc)
            await _remember_error(account_id, exc)


async def sync_trades() -> None:
    """Догрузить историю сделок по парам из списка отслеживания.

    Биржи не отдают историю «по всем парам сразу», поэтому спрашиваем
    только то, за чем пользователь следит: иначе на каждую синхронизацию
    приходились бы сотни запросов.
    """
    for account_id in await _syncable_account_ids():
        try:
            async with session_scope() as session:
                account = await session.get(ExchangeAccount, account_id)
                if account is None:
                    continue

                symbols = await _watchlist_symbols(session, account)
                if not symbols:
                    continue

                adapter = await keys_service.build_adapter(session, account)
                try:
                    count = await portfolio_service.sync_trades(
                        session, account, adapter, symbols=symbols
                    )
                    await session.commit()
                finally:
                    await adapter.close()

            if count:
                logger.info("Подключение %s: новых сделок %s", account_id, count)
        except Exception as exc:
            logger.warning("Сделки подключения %s не обновлены: %s", account_id, exc)


async def snapshot_portfolios() -> None:
    """Записать точку графика стоимости для каждого пользователя."""
    for user_id in await _active_user_ids():
        try:
            async with session_scope() as session:
                user = await session.get(User, user_id)
                if user is None:
                    continue
                snapshot = await portfolio_service.take_snapshot(session, user)
                await session.commit()
                total = snapshot.total_usd if snapshot is not None else None
            if total is not None:
                logger.debug("Снимок портфеля %s: %s", user_id, total)
        except Exception:
            logger.exception("Не удалось снять портфель пользователя %s", user_id)


# --- Списки простыми значениями ---


async def _active_exchange_codes() -> list[str]:
    async with session_scope() as session:
        result = await session.execute(
            select(Exchange.code)
            .where(Exchange.is_active.is_(True))
            .order_by(Exchange.sort_order)
        )
        return [code for (code,) in result]


async def _syncable_account_ids() -> list[int]:
    """Подключения, которые имеет смысл опрашивать.

    Ключи, признанные недействительными, пропускаем: биржа всё равно
    ответит отказом, а лимит запросов израсходуется.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(ExchangeAccount.id)
            .where(ExchangeAccount.status != KEY_STATUS_INVALID)
            .order_by(ExchangeAccount.id)
        )
        return [account_id for (account_id,) in result]


async def _active_user_ids() -> list[int]:
    async with session_scope() as session:
        result = await session.execute(
            select(User.id).where(User.is_active.is_(True)).order_by(User.id)
        )
        return [user_id for (user_id,) in result]


# --- Вспомогательное ---


async def _exchange_by_code(session, code: str) -> Exchange:
    result = await session.execute(select(Exchange).where(Exchange.code == code))
    return result.scalar_one()


async def _watchlist_symbols(session, account: ExchangeAccount) -> list[str]:
    result = await session.execute(
        select(Market.symbol)
        .join(WatchlistItem, WatchlistItem.market_id == Market.id)
        .where(
            WatchlistItem.user_id == account.user_id,
            Market.exchange_id == account.exchange_id,
        )
    )
    return [symbol for (symbol,) in result]


async def _remember_error(account_id: int, exc: Exception) -> None:
    """Записать причину сбоя отдельной сессией.

    Сессия своя, потому что предыдущая уже закрыта неудачей, а сообщение
    терять нельзя: без него пользователь не поймёт, почему портфель
    перестал обновляться.
    """
    try:
        async with session_scope() as session:
            account = await session.get(ExchangeAccount, account_id)
            if account is not None:
                await keys_service.mark_sync_error(session, account, str(exc))
                await session.commit()
    except Exception:
        logger.exception("Не удалось записать ошибку синхронизации подключения %s", account_id)


# --- Свечи, сигналы и алерты ---


async def poll_candles() -> None:
    """Держать свежими свечи по парам, которые кому-то нужны.

    Качать всё подряд нельзя: пар больше тысячи, а таблица свечей растёт
    быстро. Берём только то, на чём стоят правила сигналов, алерты и
    списки отслеживания.
    """
    targets = await _candle_targets()

    for market_id, timeframe_id in targets:
        try:
            async with session_scope() as session:
                market = await session.get(Market, market_id)
                timeframe = await session.get(Timeframe, timeframe_id)
                if market is None or timeframe is None:
                    continue
                if await candle_service.is_fresh(session, market, timeframe):
                    continue

                exchange = await session.get(Exchange, market.exchange_id)
                async with CcxtAdapter(exchange.code) as adapter:
                    await candle_service.sync_candles(session, market, timeframe, adapter)
                await session.commit()
        except Exception:
            logger.exception("Не удалось обновить свечи пары %s", market_id)


async def evaluate_signals() -> None:
    """Посчитать правила сигналов по свежим свечам."""
    async with session_scope() as session:
        rule_ids = [rule.id for rule in await signal_service.active_rules(session)]

    for rule_id in rule_ids:
        try:
            async with session_scope() as session:
                rule = await session.get(SignalRule, rule_id)
                if rule is None:
                    continue

                timeframe = await session.get(Timeframe, rule.timeframe_id)
                for market_id in await _rule_markets(session, rule):
                    market = await session.get(Market, market_id)
                    if market is None:
                        continue
                    signal = await signal_service.evaluate_rule(
                        session, rule, market, timeframe
                    )
                    if signal is not None:
                        logger.info(
                            "Сигнал %s по %s: %s",
                            signal.direction, market.symbol, signal.reason,
                        )
                await session.commit()
        except Exception:
            logger.exception("Правило сигналов %s не отработало", rule_id)


async def evaluate_signal_outcomes() -> None:
    """Проверить, сыграли ли сигналы, у которых истёк горизонт."""
    try:
        async with session_scope() as session:
            count = await signal_service.evaluate_outcomes(session)
            await session.commit()
        if count:
            logger.info("Оценено сигналов: %s", count)
    except Exception:
        logger.exception("Не удалось оценить результаты сигналов")


async def evaluate_alerts() -> None:
    """Проверить условия алертов и разослать сработавшие."""
    try:
        async with session_scope() as session:
            fired = await alert_service.evaluate_all(session)
            await session.commit()
        for trigger in fired:
            logger.info("Алерт сработал: %s", trigger.message)
    except Exception:
        logger.exception("Не удалось проверить алерты")


async def _candle_targets() -> list[tuple[int, int]]:
    """Пары и таймфреймы, по которым нужны свечи."""
    async with session_scope() as session:
        targets: set[tuple[int, int]] = set()

        rules = await session.execute(
            select(SignalRule.market_id, SignalRule.timeframe_id, SignalRule.user_id)
            .where(SignalRule.is_active.is_(True))
        )
        for market_id, timeframe_id, user_id in rules:
            if market_id is not None:
                targets.add((market_id, timeframe_id))
                continue
            # Правило без конкретной пары считается по списку отслеживания.
            watched = await session.execute(
                select(WatchlistItem.market_id).where(
                    WatchlistItem.user_id == user_id
                    if user_id is not None
                    else WatchlistItem.user_id.is_not(None)
                )
            )
            for (watched_market_id,) in watched:
                targets.add((watched_market_id, timeframe_id))

        # Алертам нужен свой таймфрейм для RSI и изменения за период.
        alert_timeframe = await session.execute(
            select(Timeframe.id).where(Timeframe.code == alert_service.ALERT_TIMEFRAME)
        )
        alert_timeframe_id = alert_timeframe.scalar_one_or_none()
        if alert_timeframe_id is not None:
            markets = await session.execute(
                select(Alert.market_id).where(Alert.is_active.is_(True)).distinct()
            )
            for (market_id,) in markets:
                targets.add((market_id, alert_timeframe_id))

        return sorted(targets)


async def _rule_markets(session, rule: SignalRule) -> list[int]:
    if rule.market_id is not None:
        return [rule.market_id]

    query = select(WatchlistItem.market_id).distinct()
    if rule.user_id is not None:
        query = query.where(WatchlistItem.user_id == rule.user_id)
    result = await session.execute(query)
    return [market_id for (market_id,) in result]
