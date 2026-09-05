"""Аккаунты: регистрация, вход, пароль, двухфакторная аутентификация.

Роутеры и хендлеры бота работают только через эти функции — проверки
доступа и записи в аудит не должны разъезжаться по слоям представления.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserRecoveryCode
from app.models.user import ROLE_OWNER, ROLE_USER
from app.services import audit_service, security

MIN_PASSWORD_LENGTH = 10
RECOVERY_CODES_COUNT = 10
TELEGRAM_CODE_TTL = timedelta(minutes=15)


class UserServiceError(Exception):
    """Базовая ошибка сервиса пользователей."""


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


# --- Поиск ---


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


# --- Пароли ---


def validate_password(password: str, *, email: str | None = None) -> None:
    """Минимальные требования к паролю.

    Намеренно без обязательных спецсимволов и цифр: такие правила гонят
    людей к «Password1!», а длину они не увеличивают. Длина здесь и есть
    основное требование.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Пароль короче {MIN_PASSWORD_LENGTH} символов.")
    if not password.strip():
        raise WeakPassword("Пароль не может состоять из пробелов.")
    if email and password.strip().lower() == normalize_email(email):
        raise WeakPassword("Пароль не должен совпадать с адресом почты.")


# --- Создание и вход ---


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
    """Проверить логин и пароль. Второй фактор проверяется отдельно.

    Любая неудача даёт одну и ту же ошибку наружу: по разнице сообщений
    иначе можно перебрать, какие адреса зарегистрированы.
    """
    email = normalize_email(email)
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


async def complete_login(
    session: AsyncSession,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Отметить успешный вход — после всех факторов, а не после пароля."""
    user.last_login_at = datetime.now(timezone.utc)
    await audit_service.log_login(
        session,
        email=user.email,
        user_id=user.id,
        is_success=True,
        ip=ip,
        user_agent=user_agent,
    )


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


# --- Двухфакторная аутентификация ---


def begin_totp_setup(user: User) -> tuple[str, str]:
    """Сгенерировать секрет и ссылку для QR.

    Секрет пока никуда не сохраняется: 2FA включится только после того,
    как пользователь подтвердит его кодом из приложения. Иначе можно
    запереть себя, отсканировав QR с ошибкой.
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
    """Включить 2FA и выдать коды восстановления.

    Коды возвращаются один раз в открытом виде — в базе только хеши.
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
    """Выключить 2FA. Требует пароль — иначе угнанная сессия снимет защиту."""
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
    """Проверить код из приложения или код восстановления.

    Код восстановления одноразовый: при удачной проверке гасится.
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


# --- Привязка Telegram ---


async def issue_telegram_link_code(session: AsyncSession, user: User) -> str:
    """Выдать одноразовый код, который пользователь отправит боту."""
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


# --- Первый запуск ---


async def ensure_owner(session: AsyncSession, *, email: str, password: str) -> User | None:
    """Создать владельца, если в базе ещё нет ни одного пользователя.

    Идемпотентно: на непустой базе не делает ничего и пароль из
    окружения не переприменяет — иначе смена SEED_OWNER_PASSWORD
    молча перетирала бы пароль, заданный владельцем в интерфейсе.
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
    """SQLite отдаёт наивные datetime — приводим к UTC для сравнения."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
