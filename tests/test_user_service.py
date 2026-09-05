from datetime import datetime, timedelta, timezone

import pyotp
import pytest

from app.models import LoginEvent, UserRecoveryCode
from app.models.user import ROLE_OWNER
from app.services import security, user_service
from sqlalchemy import select

PASSWORD = "sufficiently-long-password"


# --- Создание ---


async def test_create_user_normalizes_email(session):
    user = await user_service.create_user(
        session, email="  Owner@Example.COM ", password=PASSWORD
    )
    assert user.email == "owner@example.com"


async def test_password_is_not_stored_in_clear(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)

    assert PASSWORD not in user.password_hash
    assert security.verify_password(PASSWORD, user.password_hash)


async def test_duplicate_email_rejected(session):
    await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    with pytest.raises(user_service.EmailAlreadyUsed):
        await user_service.create_user(session, email="A@B.com", password=PASSWORD)


@pytest.mark.parametrize(
    "password",
    ["short", "         ", ""],
)
async def test_weak_passwords_rejected(session, password):
    with pytest.raises(user_service.WeakPassword):
        await user_service.create_user(session, email="a@b.com", password=password)


async def test_password_equal_to_email_rejected(session):
    with pytest.raises(user_service.WeakPassword):
        await user_service.create_user(
            session, email="longaddress@example.com", password="longaddress@example.com"
        )


# --- Вход ---


async def test_authenticate_success(session):
    created = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    user = await user_service.authenticate(session, email="A@b.com", password=PASSWORD)
    assert user.id == created.id


async def test_authenticate_wrong_password(session):
    await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate(session, email="a@b.com", password="wrong-password")


async def test_unknown_email_gives_same_error_as_wrong_password(session):
    """Сообщения не должны различаться — иначе перебираются чужие адреса."""
    await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    with pytest.raises(user_service.InvalidCredentials) as unknown:
        await user_service.authenticate(session, email="nobody@b.com", password=PASSWORD)
    with pytest.raises(user_service.InvalidCredentials) as bad_password:
        await user_service.authenticate(session, email="a@b.com", password="wrong-password")

    assert str(unknown.value) == str(bad_password.value)


async def test_inactive_account_cannot_log_in(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    user.is_active = False
    await session.commit()

    with pytest.raises(user_service.AccountInactive):
        await user_service.authenticate(session, email="a@b.com", password=PASSWORD)


async def test_failed_attempts_are_recorded(session):
    await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate(session, email="a@b.com", password="wrong-password")
    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate(session, email="ghost@b.com", password=PASSWORD)
    await session.commit()

    events = (await session.execute(select(LoginEvent))).scalars().all()
    reasons = {e.failure_reason for e in events}

    assert len(events) == 2
    assert reasons == {"bad_password", "unknown_email"}
    # Попытка по несуществующему адресу тоже оставляет след.
    assert any(e.user_id is None and e.email == "ghost@b.com" for e in events)


async def test_password_never_lands_in_login_events(session):
    await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()
    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate(session, email="a@b.com", password="secret-typo-here")
    await session.commit()

    events = (await session.execute(select(LoginEvent))).scalars().all()
    assert all("secret-typo-here" not in str(e.__dict__) for e in events)


# --- Смена пароля ---


async def test_change_password(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    await user_service.change_password(
        session, user, current_password=PASSWORD, new_password="another-good-password"
    )
    await session.commit()

    assert security.verify_password("another-good-password", user.password_hash)


async def test_change_password_requires_current(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    with pytest.raises(user_service.InvalidCredentials):
        await user_service.change_password(
            session, user, current_password="nope-nope-nope", new_password="another-good-password"
        )


# --- Двухфакторная аутентификация ---


async def test_totp_setup_flow(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    secret, uri = user_service.begin_totp_setup(user)
    assert not user.totp_enabled, "до подтверждения кодом 2FA не включается"
    assert secret in uri

    codes = await user_service.confirm_totp(
        session, user, secret=secret, code=pyotp.TOTP(secret).now()
    )
    await session.commit()

    assert user.totp_enabled
    assert len(codes) == user_service.RECOVERY_CODES_COUNT
    # В базе только хеши.
    stored = (await session.execute(select(UserRecoveryCode))).scalars().all()
    assert len(stored) == user_service.RECOVERY_CODES_COUNT
    assert all(c not in {s.code_hash for s in stored} for c in codes)


async def test_totp_secret_stored_encrypted(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    secret, _ = user_service.begin_totp_setup(user)
    await user_service.confirm_totp(session, user, secret=secret, code=pyotp.TOTP(secret).now())
    await session.commit()

    assert user.totp_secret_enc != secret
    assert security.decrypt_secret(user.totp_secret_enc) == secret


async def test_totp_wrong_code_does_not_enable(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    secret, _ = user_service.begin_totp_setup(user)

    with pytest.raises(user_service.InvalidTotpCode):
        await user_service.confirm_totp(session, user, secret=secret, code="000000")
    assert not user.totp_enabled


async def test_second_factor_accepts_totp_and_recovery_code(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    secret, _ = user_service.begin_totp_setup(user)
    codes = await user_service.confirm_totp(
        session, user, secret=secret, code=pyotp.TOTP(secret).now()
    )
    await session.commit()

    assert await user_service.verify_second_factor(session, user, pyotp.TOTP(secret).now())
    assert await user_service.verify_second_factor(session, user, codes[0])
    await session.commit()


async def test_recovery_code_works_only_once(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    secret, _ = user_service.begin_totp_setup(user)
    codes = await user_service.confirm_totp(
        session, user, secret=secret, code=pyotp.TOTP(secret).now()
    )
    await session.commit()

    assert await user_service.verify_second_factor(session, user, codes[0])
    await session.commit()
    assert not await user_service.verify_second_factor(session, user, codes[0])
    assert await user_service.unused_recovery_codes_count(session, user) == len(codes) - 1


async def test_second_factor_passes_when_2fa_off(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    assert await user_service.verify_second_factor(session, user, "не важно")


async def test_disable_totp_requires_password(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    secret, _ = user_service.begin_totp_setup(user)
    await user_service.confirm_totp(session, user, secret=secret, code=pyotp.TOTP(secret).now())
    await session.commit()

    with pytest.raises(user_service.InvalidCredentials):
        await user_service.disable_totp(session, user, password="wrong-password")

    await user_service.disable_totp(session, user, password=PASSWORD)
    await session.commit()

    assert not user.totp_enabled
    assert user.totp_secret_enc is None
    assert await user_service.unused_recovery_codes_count(session, user) == 0


# --- Привязка Telegram ---


async def test_telegram_link_flow(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    await session.commit()

    code = await user_service.issue_telegram_link_code(session, user)
    await session.commit()

    linked = await user_service.link_telegram(
        session, code=code, telegram_id=123456, telegram_username="trader"
    )
    await session.commit()

    assert linked.id == user.id
    assert user.telegram_id == 123456
    assert user.telegram_link_code is None, "код гасится после использования"


async def test_telegram_code_is_single_use(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    code = await user_service.issue_telegram_link_code(session, user)
    await session.commit()

    await user_service.link_telegram(session, code=code, telegram_id=1)
    await session.commit()

    with pytest.raises(user_service.TelegramCodeInvalid):
        await user_service.link_telegram(session, code=code, telegram_id=2)


async def test_expired_telegram_code_rejected(session):
    user = await user_service.create_user(session, email="a@b.com", password=PASSWORD)
    code = await user_service.issue_telegram_link_code(session, user)
    user.telegram_link_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    with pytest.raises(user_service.TelegramCodeInvalid):
        await user_service.link_telegram(session, code=code, telegram_id=1)


# --- Первый запуск ---


async def test_ensure_owner_creates_first_user(session):
    user = await user_service.ensure_owner(session, email="owner@example.com", password=PASSWORD)
    await session.commit()

    assert user is not None
    assert user.role == ROLE_OWNER
    assert user.is_owner


async def test_ensure_owner_is_idempotent(session):
    await user_service.ensure_owner(session, email="owner@example.com", password=PASSWORD)
    await session.commit()

    again = await user_service.ensure_owner(session, email="owner@example.com", password=PASSWORD)
    assert again is None
    assert await user_service.count_users(session) == 1


async def test_ensure_owner_does_not_reset_existing_password(session):
    """Смена SEED_OWNER_PASSWORD не должна перетирать пароль владельца."""
    owner = await user_service.ensure_owner(
        session, email="owner@example.com", password=PASSWORD
    )
    await session.commit()

    await user_service.ensure_owner(
        session, email="owner@example.com", password="completely-different"
    )
    await session.commit()

    assert security.verify_password(PASSWORD, owner.password_hash)
