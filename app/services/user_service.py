"""Accounts: registration, login, password, two-factor authentication.

Routers and bot handlers work only through these functions - access checks and audit
records must not drift apart across presentation layers.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import available_timezones

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LoginEvent, User, UserRecoveryCode
from app.models.user import ROLE_OWNER, ROLE_USER
from app.services import audit_service, security

MIN_PASSWORD_LENGTH = 10
RECOVERY_CODES_COUNT = 10
TELEGRAM_CODE_TTL = timedelta(minutes=15)

# Brute-force limit: after this many failures in a row the address is
# temporarily locked. The count is kept in login_events, so it works the same
# across all processes and survives restarts.
LOGIN_ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_FAILED_ATTEMPTS = 10


class UserServiceError(Exception):
    """Base error of the user service."""


class EmailAlreadyUsed(UserServiceError):
    pass


class WeakPassword(UserServiceError):
    pass


class InvalidCredentials(UserServiceError):
    pass


class AccountInactive(UserServiceError):
    pass


class TotpAlreadyEnabled(UserServiceError):
    pass


class InvalidTotpCode(UserServiceError):
    pass


class TelegramCodeInvalid(UserServiceError):
    pass


class TooManyAttempts(UserServiceError):
    pass


# --- Lookup ---


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == normalize_email(email)))
    return result.scalar_one_or_none()


async def get_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


# --- Passwords ---


def validate_password(password: str, *, email: str | None = None) -> None:
    """Minimum password requirements.

    Deliberately no mandatory special characters or digits: such rules push people
    towards "Password1!" without making passwords longer. Length is the main requirement
    here.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Пароль короче {MIN_PASSWORD_LENGTH} символов.")
    if not password.strip():
        raise WeakPassword("Пароль не может состоять из пробелов.")
    if email and password.strip().lower() == normalize_email(email):
        raise WeakPassword("Пароль не должен совпадать с адресом почты.")


# --- Creation and login ---


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    role: str = ROLE_USER,
) -> User:
    email = normalize_email(email)
    validate_password(password, email=email)

    if await get_by_email(session, email) is not None:
        raise EmailAlreadyUsed("Пользователь с таким адресом уже есть.")

    user = User(
        email=email,
        password_hash=security.hash_password(password),
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> User:
    """Check the login and password. The second factor is checked separately.

    Every failure produces the same error to the outside: otherwise differences in
    messages would let someone enumerate which addresses are registered.
    """
    email = normalize_email(email)
    await check_login_throttle(session, email)

    user = await get_by_email(session, email)

    if user is None:
        await audit_service.log_login(
            session,
            email=email,
            is_success=False,
            failure_reason=audit_service.FAILURE_UNKNOWN_EMAIL,
            ip=ip,
            user_agent=user_agent,
        )
        raise InvalidCredentials("Неверный адрес или пароль.")

    if not security.verify_password(password, user.password_hash):
        await audit_service.log_login(
            session,
            email=email,
            user_id=user.id,
            is_success=False,
            failure_reason=audit_service.FAILURE_BAD_PASSWORD,
            ip=ip,
            user_agent=user_agent,
        )
        raise InvalidCredentials("Неверный адрес или пароль.")

    if not user.is_active:
        await audit_service.log_login(
            session,
            email=email,
            user_id=user.id,
            is_success=False,
            failure_reason=audit_service.FAILURE_INACTIVE,
            ip=ip,
            user_agent=user_agent,
        )
        raise AccountInactive("Учётная запись отключена.")

    return user


async def recent_failed_attempts(session: AsyncSession, email: str) -> int:
    """Failed attempts within the window and after the last successful login.

    The cutoff is by event id, not time: in PostgreSQL now() returns the transaction
    start time, so events from one request get the same timestamp and comparing by time
    becomes ambiguous. Ids are monotonic and have no such ambiguity.
    """
    email = normalize_email(email)

    last_success_id = await session.scalar(
        select(func.max(LoginEvent.id)).where(
            LoginEvent.email == email,
            LoginEvent.is_success.is_(True),
        )
    )

    query = select(func.count()).select_from(LoginEvent).where(
        LoginEvent.email == email,
        LoginEvent.is_success.is_(False),
        LoginEvent.created_at >= datetime.now(timezone.utc) - LOGIN_ATTEMPT_WINDOW,
    )
    if last_success_id is not None:
        query = query.where(LoginEvent.id > last_success_id)

    return int(await session.scalar(query))


async def check_login_throttle(session: AsyncSession, email: str) -> None:
    """Block the password check after a series of failures.

    Counted per address, not per IP: otherwise brute-forcing with rotating egress nodes
    slips past the limit. The count starts from the last successful login - otherwise
    old typos would accumulate and one day lock out the account owner out of nowhere.
    """
    if await recent_failed_attempts(session, email) >= MAX_FAILED_ATTEMPTS:
        raise TooManyAttempts(
            "Слишком много неудачных попыток. Попробуйте через 15 минут."
        )


async def complete_login(
    session: AsyncSession,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Mark a successful login - after all factors, not after the password."""
    user.last_login_at = datetime.now(timezone.utc)
    await audit_service.log_login(
        session,
        email=user.email,
        user_id=user.id,
        is_success=True,
        ip=ip,
        user_agent=user_agent,
    )


async def set_timezone(session: AsyncSession, user: User, name: str) -> None:
    """Change the display time zone.

    The name is checked against the zone database, not against the list in the form: the
    UI list may lag behind, and an unknown name would silently put the user back on UTC -
    without them noticing.
    """
    name = (name or "").strip()
    if name not in available_timezones():
        raise UserServiceError("Такого часового пояса нет.")

    user.timezone = name
    await session.flush()


async def change_password(
    session: AsyncSession,
    user: User,
    *,
    current_password: str,
    new_password: str,
) -> None:
    if not security.verify_password(current_password, user.password_hash):
        raise InvalidCredentials("Текущий пароль указан неверно.")

    validate_password(new_password, email=user.email)
    user.password_hash = security.hash_password(new_password)
    await audit_service.log_action(
        session,
        action=audit_service.ACTION_PASSWORD_CHANGED,
        user_id=user.id,
        entity="user",
        entity_id=user.id,
    )


# --- Two-factor authentication ---


def begin_totp_setup(user: User) -> tuple[str, str]:
    """Generate a secret and a link for the QR code.

    The secret isn't stored anywhere yet: 2FA is enabled only after the user confirms it
    with a code from the app. Otherwise one could lock themselves out by scanning the QR
    code wrong.
    """
    if user.totp_enabled:
        raise TotpAlreadyEnabled("Двухфакторная аутентификация уже включена.")

    secret = security.generate_totp_secret()
    uri = security.totp_provisioning_uri(secret, user.email, "Crypto Hub")
    return secret, uri


async def confirm_totp(
    session: AsyncSession,
    user: User,
    *,
    secret: str,
    code: str,
) -> list[str]:
    """Enable 2FA and issue recovery codes.

    The codes are returned once in plain text - only hashes are stored in the database.
    """
    if user.totp_enabled:
        raise TotpAlreadyEnabled("Двухфакторная аутентификация уже включена.")
    if not security.verify_totp(secret, code):
        raise InvalidTotpCode("Код не подошёл. Проверьте время на устройстве.")

    user.totp_secret_enc = security.encrypt_secret(secret)
    user.totp_enabled = True

    codes = await _reset_recovery_codes(session, user)
    await audit_service.log_action(
        session,
        action=audit_service.ACTION_TOTP_ENABLED,
        user_id=user.id,
        entity="user",
        entity_id=user.id,
    )
    return codes


async def disable_totp(session: AsyncSession, user: User, *, password: str) -> None:
    """Disable 2FA. Needs the password, or a hijacked session could remove protection."""
    if not security.verify_password(password, user.password_hash):
        raise InvalidCredentials("Пароль указан неверно.")

    user.totp_enabled = False
    user.totp_secret_enc = None
    await _delete_recovery_codes(session, user)
    await audit_service.log_action(
        session,
        action=audit_service.ACTION_TOTP_DISABLED,
        user_id=user.id,
        entity="user",
        entity_id=user.id,
    )


async def verify_second_factor(session: AsyncSession, user: User, code: str) -> bool:
    """Verify a code from the app or a recovery code.

    A recovery code is single-use: it is invalidated on successful verification.
    """
    if not user.totp_enabled or not user.totp_secret_enc:
        return True

    secret = security.decrypt_secret(user.totp_secret_enc)
    if security.verify_totp(secret, code):
        return True

    return await _consume_recovery_code(session, user, code)


async def _reset_recovery_codes(session: AsyncSession, user: User) -> list[str]:
    await _delete_recovery_codes(session, user)

    codes = [security.generate_recovery_code() for _ in range(RECOVERY_CODES_COUNT)]
    for code in codes:
        session.add(
            UserRecoveryCode(user_id=user.id, code_hash=security.hash_recovery_code(code))
        )
    await session.flush()
    return codes


async def _delete_recovery_codes(session: AsyncSession, user: User) -> None:
    result = await session.execute(
        select(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id)
    )
    for row in result.scalars():
        await session.delete(row)
    await session.flush()


async def _consume_recovery_code(session: AsyncSession, user: User, code: str) -> bool:
    result = await session.execute(
        select(UserRecoveryCode).where(
            UserRecoveryCode.user_id == user.id,
            UserRecoveryCode.used_at.is_(None),
        )
    )
    for row in result.scalars():
        if security.verify_recovery_code(code, row.code_hash):
            row.used_at = datetime.now(timezone.utc)
            await audit_service.log_action(
                session,
                action=audit_service.ACTION_RECOVERY_CODE_USED,
                user_id=user.id,
                entity="user",
                entity_id=user.id,
            )
            await session.flush()
            return True
    return False


async def unused_recovery_codes_count(session: AsyncSession, user: User) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(UserRecoveryCode)
        .where(
            UserRecoveryCode.user_id == user.id,
            UserRecoveryCode.used_at.is_(None),
        )
    )
    return int(result.scalar_one())


# --- Telegram linking ---


async def issue_telegram_link_code(session: AsyncSession, user: User) -> str:
    """Issue a one-time code that the user will send to the bot."""
    code = security.generate_numeric_code(8)
    user.telegram_link_code = code
    user.telegram_link_expires_at = datetime.now(timezone.utc) + TELEGRAM_CODE_TTL
    await session.flush()
    return code


async def link_telegram(
    session: AsyncSession,
    *,
    code: str,
    telegram_id: int,
    telegram_username: str | None = None,
) -> User:
    result = await session.execute(
        select(User).where(User.telegram_link_code == code.strip())
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise TelegramCodeInvalid("Код не найден. Сгенерируйте новый в веб-панели.")

    expires_at = user.telegram_link_expires_at
    if expires_at is None or _as_utc(expires_at) < datetime.now(timezone.utc):
        raise TelegramCodeInvalid("Код истёк. Сгенерируйте новый в веб-панели.")

    user.telegram_id = telegram_id
    user.telegram_username = telegram_username
    user.telegram_link_code = None
    user.telegram_link_expires_at = None

    await audit_service.log_action(
        session,
        action=audit_service.ACTION_TELEGRAM_LINKED,
        user_id=user.id,
        entity="user",
        entity_id=user.id,
        payload={"telegram_id": telegram_id},
    )
    await session.flush()
    return user


# --- First start ---


async def ensure_owner(session: AsyncSession, *, email: str, password: str) -> User | None:
    """Create the owner if the database has no users yet.

    Idempotent: on a non-empty database it does nothing and doesn't reapply the password
    from the environment - otherwise changing SEED_OWNER_PASSWORD would silently
    overwrite the password the owner set in the UI.
    """
    if await count_users(session) > 0:
        return None

    user = await create_user(session, email=email, password=password, role=ROLE_OWNER)
    await audit_service.log_action(
        session,
        action=audit_service.ACTION_USER_REGISTERED,
        user_id=user.id,
        entity="user",
        entity_id=user.id,
        payload={"role": ROLE_OWNER, "source": "seed"},
    )
    return user


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes - convert to UTC for comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
