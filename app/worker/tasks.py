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
from app.models import Exchange, ExchangeAccount, Market, User, WatchlistItem
from app.models.exchange import KEY_STATUS_INVALID
from app.services import exchange_keys_service as keys_service
from app.services import market_service, portfolio_service

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
