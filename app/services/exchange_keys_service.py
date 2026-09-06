"""Подключённые ключи бирж.

Ключ проверяется у биржи до сохранения и хранится только зашифрованным.
Право на торговлю выставляется исключительно по подтверждению биржи —
галочка в форме сама по себе его не даёт.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.base import ExchangeAdapter, KeyCheck
from app.exchanges.ccxt_client import CcxtAdapter
from app.models import Exchange, ExchangeAccount, User
from app.models.exchange import (
    KEY_STATUS_ERROR,
    KEY_STATUS_INVALID,
    KEY_STATUS_OK,
)
from app.services import audit_service, security

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[str, str, str, bool], ExchangeAdapter]


class ExchangeKeyError(Exception):
    """Базовая ошибка работы с ключами бирж."""


class ExchangeNotSupported(ExchangeKeyError):
    pass


class KeyRejected(ExchangeKeyError):
    """Биржа не приняла ключ."""


class AccountNotFound(ExchangeKeyError):
    pass


def default_adapter_factory(
    exchange_code: str,
    api_key: str,
    api_secret: str,
    testnet: bool,
) -> ExchangeAdapter:
    return CcxtAdapter(
        exchange_code,
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
    )


# --- Чтение ---


async def list_accounts(session: AsyncSession, user: User) -> list[ExchangeAccount]:
    result = await session.execute(
        select(ExchangeAccount)
        .where(ExchangeAccount.user_id == user.id)
        .order_by(ExchangeAccount.id)
    )
    return list(result.scalars())


async def get_account(session: AsyncSession, user: User, account_id: int) -> ExchangeAccount:
    """Достать ключ с проверкой владельца.

    Фильтр по user_id стоит здесь, а не в роутере: иначе один забытый
    роутер открывает чужие ключи.
    """
    result = await session.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.id == account_id,
            ExchangeAccount.user_id == user.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise AccountNotFound("Подключение не найдено.")
    return account


async def get_exchange_by_code(session: AsyncSession, code: str) -> Exchange:
    result = await session.execute(select(Exchange).where(Exchange.code == code))
    exchange = result.scalar_one_or_none()
    if exchange is None or not exchange.is_active:
        raise ExchangeNotSupported(f"Биржа {code} недоступна.")
    return exchange


async def list_exchanges(session: AsyncSession) -> list[Exchange]:
    result = await session.execute(
        select(Exchange).where(Exchange.is_active.is_(True)).order_by(Exchange.sort_order)
    )
    return list(result.scalars())


# --- Подключение ---


async def add_account(
    session: AsyncSession,
    user: User,
    *,
    exchange_code: str,
    api_key: str,
    api_secret: str,
    label: str = "main",
    testnet: bool = False,
    want_trading: bool = False,
    adapter_factory: AdapterFactory = default_adapter_factory,
) -> ExchangeAccount:
    """Проверить ключ у биржи и сохранить его зашифрованным.

    Непринятый ключ в базу не попадает: хранить заведомо нерабочие
    учётные данные незачем, а пользователю нужна ошибка сразу.
    """
    exchange = await get_exchange_by_code(session, exchange_code)

    api_key = api_key.strip()
    api_secret = api_secret.strip()
    if not api_key or not api_secret:
        raise KeyRejected("Заполните и ключ, и секрет.")

    # Признак хранится в справочнике бирж, чтобы клиент мог добавить туда
    # биржу без песочницы, не трогая код. Проверяем до запроса к бирже:
    # обращаться в несуществующую тестовую сеть незачем.
    if testnet and not exchange.supports_testnet:
        raise KeyRejected(f"У биржи {exchange.name} нет тестовой сети.")

    check = await _check_with_exchange(exchange_code, api_key, api_secret, testnet, adapter_factory)
    if not check.is_valid:
        raise KeyRejected(check.error or "Биржа отклонила ключ.")

    account = ExchangeAccount(
        user_id=user.id,
        exchange_id=exchange.id,
        label=label.strip() or "main",
        api_key_enc=security.encrypt_secret(api_key),
        api_secret_enc=security.encrypt_secret(api_secret),
        api_key_masked=mask_key(api_key),
        is_testnet=testnet,
        requested_trading=want_trading,
        allow_trading=want_trading and check.can_trade,
        status=KEY_STATUS_OK,
        checked_at=datetime.now(timezone.utc),
    )
    session.add(account)
    await session.flush()

    await audit_service.log_action(
        session,
        action=audit_service.ACTION_EXCHANGE_KEY_ADDED,
        user_id=user.id,
        entity="exchange_account",
        entity_id=account.id,
        # В журнал уходит только маска — не сам ключ.
        payload={
            "exchange": exchange_code,
            "masked": account.api_key_masked,
            "testnet": testnet,
            "trading": account.allow_trading,
        },
    )
    if account.allow_trading:
        await audit_service.log_action(
            session,
            action=audit_service.ACTION_TRADING_ENABLED,
            user_id=user.id,
            entity="exchange_account",
            entity_id=account.id,
        )
    return account


async def recheck_account(
    session: AsyncSession,
    account: ExchangeAccount,
    *,
    adapter_factory: AdapterFactory = default_adapter_factory,
) -> KeyCheck:
    """Перепроверить ключ: права могли отозвать на стороне биржи."""
    exchange = await session.get(Exchange, account.exchange_id)
    api_key, api_secret = decrypt_credentials(account)

    check = await _check_with_exchange(
        exchange.code, api_key, api_secret, account.is_testnet, adapter_factory
    )

    account.checked_at = datetime.now(timezone.utc)
    if check.is_valid:
        account.status = KEY_STATUS_OK
        account.last_error = None
        account.allow_trading = account.requested_trading and check.can_trade
    else:
        account.status = KEY_STATUS_INVALID
        account.last_error = check.error
        # Ключ не подтверждён — торговать по нему нельзя, что бы ни было
        # записано раньше.
        account.allow_trading = False

    await session.flush()
    return check


async def delete_account(session: AsyncSession, user: User, account: ExchangeAccount) -> None:
    await audit_service.log_action(
        session,
        action=audit_service.ACTION_EXCHANGE_KEY_REMOVED,
        user_id=user.id,
        entity="exchange_account",
        entity_id=account.id,
        payload={"masked": account.api_key_masked},
    )
    await session.delete(account)
    await session.flush()


# --- Работа с сохранённым ключом ---


def decrypt_credentials(account: ExchangeAccount) -> tuple[str, str]:
    return (
        security.decrypt_secret(account.api_key_enc),
        security.decrypt_secret(account.api_secret_enc),
    )


async def build_adapter(
    session: AsyncSession,
    account: ExchangeAccount,
    *,
    adapter_factory: AdapterFactory = default_adapter_factory,
) -> ExchangeAdapter:
    """Собрать подключение к бирже по сохранённому ключу."""
    exchange = await session.get(Exchange, account.exchange_id)
    api_key, api_secret = decrypt_credentials(account)
    return adapter_factory(exchange.code, api_key, api_secret, account.is_testnet)


def mask_key(api_key: str) -> str:
    """Хвост ключа для опознания в интерфейсе.

    Показываем только последние символы: по началу ключа биржи иногда
    можно определить аккаунт, по четырём последним — нет.
    """
    if len(api_key) <= 4:
        return "*" * len(api_key)
    return f"…{api_key[-4:]}"


async def _check_with_exchange(
    exchange_code: str,
    api_key: str,
    api_secret: str,
    testnet: bool,
    adapter_factory: AdapterFactory,
) -> KeyCheck:
    adapter = adapter_factory(exchange_code, api_key, api_secret, testnet)
    try:
        return await adapter.check_key()
    except Exception as exc:
        logger.warning("Проверка ключа %s не удалась: %s", exchange_code, exc)
        return KeyCheck(is_valid=False, error=str(exc))
    finally:
        await adapter.close()


async def mark_sync_error(
    session: AsyncSession, account: ExchangeAccount, message: str
) -> None:
    """Отметить сбой синхронизации, не трогая признак валидности ключа."""
    account.status = KEY_STATUS_ERROR
    account.last_error = message[:1000]
    await session.flush()


async def mark_synced(session: AsyncSession, account: ExchangeAccount) -> None:
    account.status = KEY_STATUS_OK
    account.last_error = None
    account.last_sync_at = datetime.now(timezone.utc)
    await session.flush()
