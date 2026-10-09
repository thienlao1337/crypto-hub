"""Invites: registration is closed.

Codes are issued by the owner. Each code is single-use, may be tied to a specific
address and has an expiry.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invite, User
from app.services import audit_service, security, user_service

DEFAULT_TTL_DAYS = 14


class InviteError(Exception):
    """Base error of the invite service."""


class InviteNotFound(InviteError):
    pass


class InviteAlreadyUsed(InviteError):
    pass


class InviteExpired(InviteError):
    pass


class InviteRevoked(InviteError):
    pass


class InviteEmailMismatch(InviteError):
    pass


async def create_invite(
    session: AsyncSession,
    *,
    created_by: User,
    email: str | None = None,
    note: str | None = None,
    ttl_days: int | None = DEFAULT_TTL_DAYS,
) -> Invite:
    invite = Invite(
        code=security.generate_token(24),
        created_by_user_id=created_by.id,
        email=user_service.normalize_email(email) if email else None,
        note=note,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=ttl_days) if ttl_days else None
        ),
    )
    session.add(invite)
    await session.flush()

    await audit_service.log_action(
        session,
        action=audit_service.ACTION_INVITE_CREATED,
        user_id=created_by.id,
        entity="invite",
        entity_id=invite.id,
        payload={"email": invite.email},
    )
    return invite


async def get_by_code(session: AsyncSession, code: str, *, lock: bool = False) -> Invite | None:
    query = select(Invite).where(Invite.code == code.strip())
    if lock:
        # Row lock for the duration of registration: without it two
        # simultaneous requests with the same code would create two users.
        query = query.with_for_update()
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_invites(session: AsyncSession) -> list[Invite]:
    result = await session.execute(select(Invite).order_by(Invite.created_at.desc()))
    return list(result.scalars())


def check_usable(invite: Invite, *, email: str | None = None) -> None:
    """Check that the code can still be used.

    Separate from loading so the registration form can be shown with the same check as
    the submission.
    """
    if invite.revoked_at is not None:
        raise InviteRevoked("Приглашение отозвано.")
    if invite.used_at is not None:
        raise InviteAlreadyUsed("Приглашение уже использовано.")
    if invite.expires_at is not None and _as_utc(invite.expires_at) < datetime.now(timezone.utc):
        raise InviteExpired("Срок действия приглашения истёк.")
    if invite.email and email and user_service.normalize_email(email) != invite.email:
        raise InviteEmailMismatch("Приглашение выдано на другой адрес почты.")


async def validate_code(session: AsyncSession, code: str, *, email: str | None = None) -> Invite:
    invite = await get_by_code(session, code)
    if invite is None:
        raise InviteNotFound("Приглашение не найдено.")
    check_usable(invite, email=email)
    return invite


async def revoke_invite(session: AsyncSession, invite: Invite, *, by: User) -> None:
    if invite.used_at is not None:
        raise InviteAlreadyUsed("Использованное приглашение отозвать нельзя.")

    invite.revoked_at = datetime.now(timezone.utc)
    await audit_service.log_action(
        session,
        action=audit_service.ACTION_INVITE_REVOKED,
        user_id=by.id,
        entity="invite",
        entity_id=invite.id,
    )
    await session.flush()


async def register_by_invite(
    session: AsyncSession,
    *,
    code: str,
    email: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> User:
    """Create an account from an invite and invalidate the code."""
    invite = await get_by_code(session, code, lock=True)
    if invite is None:
        raise InviteNotFound("Приглашение не найдено.")
    check_usable(invite, email=email)

    user = await user_service.create_user(session, email=email, password=password)

    invite.used_at = datetime.now(timezone.utc)
    invite.used_by_user_id = user.id

    await audit_service.log_action(
        session,
        action=audit_service.ACTION_USER_REGISTERED,
        user_id=user.id,
        entity="user",
        entity_id=user.id,
        payload={"invite_id": invite.id},
        ip=ip,
        user_agent=user_agent,
    )
    await audit_service.log_action(
        session,
        action=audit_service.ACTION_INVITE_REDEEMED,
        user_id=user.id,
        entity="invite",
        entity_id=invite.id,
    )
    await session.flush()
    return user


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes - convert to UTC for comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
