"""Connected exchange keys.

A key is verified with the exchange before saving and stored only encrypted. Trading
permission is set solely on the exchange's confirmation - a checkbox in the form doesn't
grant it by itself.
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
    """Base error for exchange key operations."""


class ExchangeNotSupported(ExchangeKeyError):
    pass


class KeyRejected(ExchangeKeyError):
    """The exchange rejected the key."""


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


# --- Reading ---


async def list_accounts(session: AsyncSession, user: User) -> list[ExchangeAccount]:
    result = await session.execute(
        select(ExchangeAccount)
        .where(ExchangeAccount.user_id == user.id)
        .order_by(ExchangeAccount.id)
    )
    return list(result.scalars())


async def get_account(session: AsyncSession, user: User, account_id: int) -> ExchangeAccount:
    """Fetch a key, checking its owner.

    The user_id filter lives here, not in the router: otherwise a single forgotten
    router would expose other people's keys.
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


# --- Connecting ---


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
    """Verify the key with the exchange and store it encrypted.

    A rejected key never reaches the database: there's no point storing credentials
    known to be broken, and the user needs the error right away.
    """
    exchange = await get_exchange_by_code(session, exchange_code)

    api_key = api_key.strip()
    api_secret = api_secret.strip()
    if not api_key or not api_secret:
        raise KeyRejected("Заполните и ключ, и секрет.")

    # The flag is stored in the exchange reference table so the client can add
    # an exchange without a sandbox there without touching code. We check
    # before calling the exchange: there's no point contacting a test network
    # that doesn't exist.
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
        # Only the mask goes to the log - never the key itself.
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
    """Re-check the key: permissions may have been revoked on the exchange side."""
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
        # The key isn't confirmed - it must not be used for trading, whatever
        # was recorded before.
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


# --- Working with a stored key ---


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
    """Build an exchange connection from a stored key."""
    exchange = await session.get(Exchange, account.exchange_id)
    api_key, api_secret = decrypt_credentials(account)
    return adapter_factory(exchange.code, api_key, api_secret, account.is_testnet)


def mask_key(api_key: str) -> str:
    """Tail of the key for recognizing it in the UI.

    We show only the last characters: the beginning of an exchange key can sometimes
    identify the account, the last four can't.
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
        logger.warning("Key check for %s failed: %s", exchange_code, exc)
        return KeyCheck(is_valid=False, error=str(exc))
    finally:
        await adapter.close()


async def mark_sync_error(
    session: AsyncSession, account: ExchangeAccount, message: str
) -> None:
    """Record a sync failure without touching the key's validity flag."""
    account.status = KEY_STATUS_ERROR
    account.last_error = message[:1000]
    await session.flush()


async def mark_synced(session: AsyncSession, account: ExchangeAccount) -> None:
    account.status = KEY_STATUS_OK
    account.last_error = None
    account.last_sync_at = datetime.now(timezone.utc)
    await session.flush()
