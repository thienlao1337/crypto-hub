import pytest
import pytest_asyncio

from app.exchanges.base import KeyCheck
from app.models import AuditLog, Exchange, ExchangeAccount
from app.models.exchange import KEY_STATUS_INVALID, KEY_STATUS_OK
from app.services import exchange_keys_service as keys
from app.services import security, user_service
from sqlalchemy import select
from tests import fakes

API_KEY = "bybit-api-key-abcd1234"
API_SECRET = "bybit-api-secret-value"


@pytest_asyncio.fixture
async def exchange(session):
    """The exchange reference table is created by hand in tests: it lives in a migration."""
    row = Exchange(code="bybit", name="Bybit", is_active=True, supports_testnet=True)
    session.add(row)
    await session.commit()
    return row


@pytest_asyncio.fixture
async def user(session):
    row = await user_service.create_user(
        session, email="trader@example.com", password="trader-password-1"
    )
    await session.commit()
    return row


# --- Connecting a key ---


async def test_key_is_stored_encrypted(session, exchange, user):
    adapter = fakes.FakeAdapter()

    account = await keys.add_account(
        session,
        user,
        exchange_code="bybit",
        api_key=API_KEY,
        api_secret=API_SECRET,
        adapter_factory=fakes.factory_for(adapter),
    )
    await session.commit()

    assert API_KEY not in account.api_key_enc
    assert API_SECRET not in account.api_secret_enc
    assert security.decrypt_secret(account.api_key_enc) == API_KEY
    assert security.decrypt_secret(account.api_secret_enc) == API_SECRET
    assert adapter.closed, "подключение к бирже должно закрываться"


async def test_masked_key_hides_beginning(session, exchange, user):
    account = await keys.add_account(
        session,
        user,
        exchange_code="bybit",
        api_key=API_KEY,
        api_secret=API_SECRET,
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )

    assert account.api_key_masked == "…1234"
    assert API_KEY[:8] not in account.api_key_masked


async def _stored_accounts_count(session) -> int:
    """Count with a direct query: after rollback the session objects expire."""
    result = await session.execute(select(ExchangeAccount))
    return len(result.scalars().all())


async def test_rejected_key_is_not_saved(session, exchange, user):
    adapter = fakes.FakeAdapter(
        key_check=KeyCheck(is_valid=False, error="Ключ отклонён биржей")
    )

    with pytest.raises(keys.KeyRejected):
        await keys.add_account(
            session,
            user,
            exchange_code="bybit",
            api_key=API_KEY,
            api_secret=API_SECRET,
            adapter_factory=fakes.factory_for(adapter),
        )
    await session.rollback()

    assert await _stored_accounts_count(session) == 0


async def test_network_failure_does_not_save_key(session, exchange, user):
    """A connection failure must not result in saving an unverified key."""
    adapter = fakes.FakeAdapter(raise_on="check_key")

    with pytest.raises(keys.KeyRejected):
        await keys.add_account(
            session,
            user,
            exchange_code="bybit",
            api_key=API_KEY,
            api_secret=API_SECRET,
            adapter_factory=fakes.factory_for(adapter),
        )
    await session.rollback()

    assert await _stored_accounts_count(session) == 0
    assert adapter.closed


# --- Trading permissions ---


async def test_trading_requires_both_request_and_exchange_confirmation(session, exchange, user):
    confirmed = fakes.FakeAdapter(
        key_check=KeyCheck(is_valid=True, can_trade=True, permissions_known=True)
    )

    wanted_and_confirmed = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        label="a", want_trading=True, adapter_factory=fakes.factory_for(confirmed),
    )
    assert wanted_and_confirmed.allow_trading

    not_wanted = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        label="b", want_trading=False, adapter_factory=fakes.factory_for(confirmed),
    )
    assert not not_wanted.allow_trading, "не просили торговлю — не выдаём"


async def test_trading_denied_when_exchange_does_not_confirm(session, exchange, user):
    """The key works, but the exchange didn't confirm trading permission."""
    adapter = fakes.FakeAdapter(
        key_check=KeyCheck(is_valid=True, can_trade=False, permissions_known=True)
    )

    account = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        want_trading=True, adapter_factory=fakes.factory_for(adapter),
    )

    assert account.requested_trading
    assert not account.allow_trading
    assert not account.can_trade


async def test_trading_denied_when_permissions_unknown(session, exchange, user):
    """Permissions couldn't be determined - trading is not enabled."""
    adapter = fakes.FakeAdapter(
        key_check=KeyCheck(is_valid=True, can_trade=False, permissions_known=False)
    )

    account = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        want_trading=True, adapter_factory=fakes.factory_for(adapter),
    )

    assert not account.allow_trading


async def test_recheck_revokes_trading_when_key_goes_bad(session, exchange, user):
    good = fakes.FakeAdapter(key_check=KeyCheck(is_valid=True, can_trade=True, permissions_known=True))
    account = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        want_trading=True, adapter_factory=fakes.factory_for(good),
    )
    await session.commit()
    assert account.allow_trading

    revoked = fakes.FakeAdapter(key_check=KeyCheck(is_valid=False, error="Ключ отозван"))
    await keys.recheck_account(session, account, adapter_factory=fakes.factory_for(revoked))
    await session.commit()

    assert account.status == KEY_STATUS_INVALID
    assert not account.allow_trading
    assert account.last_error == "Ключ отозван"


async def test_recheck_restores_status(session, exchange, user):
    account = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )
    await session.commit()

    await keys.recheck_account(
        session, account, adapter_factory=fakes.factory_for(fakes.FakeAdapter())
    )
    assert account.status == KEY_STATUS_OK
    assert account.last_error is None


# --- Isolation and the log ---


async def test_other_users_key_is_not_accessible(session, exchange, user):
    account = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )
    await session.commit()

    stranger = await user_service.create_user(
        session, email="stranger@example.com", password="stranger-password-1"
    )
    await session.commit()

    with pytest.raises(keys.AccountNotFound):
        await keys.get_account(session, stranger, account.id)

    # But the owner has access.
    assert (await keys.get_account(session, user, account.id)).id == account.id


async def test_secret_never_lands_in_audit_log(session, exchange, user):
    await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )
    await session.commit()

    entries = (await session.execute(select(AuditLog))).scalars().all()
    dumped = str([e.payload for e in entries])

    assert entries
    assert API_KEY not in dumped
    assert API_SECRET not in dumped
    assert "…1234" in dumped


async def test_adapter_receives_decrypted_credentials(session, exchange, user):
    account = await keys.add_account(
        session, user, exchange_code="bybit", api_key=API_KEY, api_secret=API_SECRET,
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )
    await session.commit()

    adapter = fakes.FakeAdapter()
    built = await keys.build_adapter(
        session, account, adapter_factory=fakes.factory_for(adapter)
    )

    assert built is adapter
    assert adapter.last_args == ("bybit", API_KEY, API_SECRET, False)


async def test_unsupported_exchange_rejected(session, user):
    with pytest.raises(keys.ExchangeNotSupported):
        await keys.add_account(
            session, user, exchange_code="kraken", api_key=API_KEY, api_secret=API_SECRET,
            adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
        )


@pytest.mark.parametrize(
    ("api_key", "expected"),
    [("abcdefgh", "…efgh"), ("abcd", "****"), ("ab", "**"), ("", "")],
)
def test_mask_key(api_key, expected):
    assert keys.mask_key(api_key) == expected


async def test_testnet_refused_for_exchange_without_sandbox(session, exchange, user):
    """The sandbox flag lives in the reference table so the client can edit it themselves."""
    exchange.supports_testnet = False
    await session.flush()

    adapter = fakes.FakeAdapter()
    with pytest.raises(keys.KeyRejected):
        await keys.add_account(
            session,
            user,
            exchange_code="bybit",
            api_key=API_KEY,
            api_secret=API_SECRET,
            testnet=True,
            adapter_factory=fakes.factory_for(adapter),
        )

    # It must not get as far as the exchange: there's no point querying a
    # network that doesn't exist.
    assert adapter.calls == []


async def test_testnet_allowed_where_supported(session, exchange, user):
    account = await keys.add_account(
        session,
        user,
        exchange_code="bybit",
        api_key=API_KEY,
        api_secret=API_SECRET,
        testnet=True,
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )

    assert account.is_testnet is True
