"""Запись значимых действий и попыток входа.

Вызывается из роутеров и сервисов там, где событие должно остаться в
истории. Ничего не возвращает наружу и никогда не пишет секреты.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, LoginEvent

# Действия, которые обязаны быть восстановимы постфактум.
ACTION_USER_REGISTERED = "user.registered"
ACTION_PASSWORD_CHANGED = "user.password_changed"
ACTION_TOTP_ENABLED = "user.totp_enabled"
ACTION_TOTP_DISABLED = "user.totp_disabled"
ACTION_RECOVERY_CODE_USED = "user.recovery_code_used"
ACTION_TELEGRAM_LINKED = "user.telegram_linked"
ACTION_INVITE_CREATED = "invite.created"
ACTION_INVITE_REVOKED = "invite.revoked"
ACTION_INVITE_REDEEMED = "invite.redeemed"
ACTION_EXCHANGE_KEY_ADDED = "exchange_key.added"
ACTION_EXCHANGE_KEY_REMOVED = "exchange_key.removed"
ACTION_TRADING_ENABLED = "exchange_key.trading_enabled"
ACTION_STRATEGY_WENT_LIVE = "strategy.live_confirmed"

FAILURE_UNKNOWN_EMAIL = "unknown_email"
FAILURE_BAD_PASSWORD = "bad_password"
FAILURE_INACTIVE = "inactive"
FAILURE_BAD_SECOND_FACTOR = "bad_second_factor"


async def log_action(
    session: AsyncSession,
    *,
    action: str,
    user_id: int | None = None,
    entity: str | None = None,
    entity_id: int | None = None,
    payload: dict | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        payload=payload,
        ip=ip,
        user_agent=_trim(user_agent, 255),
    )
    session.add(entry)
    await session.flush()
    return entry


async def log_login(
    session: AsyncSession,
    *,
    email: str,
    is_success: bool,
    user_id: int | None = None,
    failure_reason: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> LoginEvent:
    """Пишем и удачные, и неудачные попытки.

    Неудачные — по email, даже когда такого пользователя нет: иначе
    перебор чужих адресов не оставит следов.
    """
    event = LoginEvent(
        user_id=user_id,
        email=email,
        is_success=is_success,
        failure_reason=failure_reason,
        ip=ip,
        user_agent=_trim(user_agent, 255),
    )
    session.add(event)
    await session.flush()
    return event


async def recent_logins(session: AsyncSession, user_id: int, limit: int = 20) -> list[LoginEvent]:
    result = await session.execute(
        select(LoginEvent)
        .where(LoginEvent.user_id == user_id)
        .order_by(LoginEvent.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def recent_actions(session: AsyncSession, user_id: int, limit: int = 50) -> list[AuditLog]:
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.user_id == user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


def _trim(value: str | None, length: int) -> str | None:
    """User-Agent приходит произвольной длины и не должен ронять вставку."""
    if value is None:
        return None
    return value[:length]
