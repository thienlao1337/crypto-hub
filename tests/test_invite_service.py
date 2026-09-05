from datetime import datetime, timedelta, timezone

import pytest

from app.services import invite_service, user_service

PASSWORD = "sufficiently-long-password"


async def test_create_invite(session, owner):
    invite = await invite_service.create_invite(session, created_by=owner, note="для брата")
    await session.commit()

    assert invite.code
    assert invite.created_by_user_id == owner.id
    assert invite.used_at is None
    assert invite.expires_at is not None


async def test_register_by_invite(session, owner):
    invite = await invite_service.create_invite(session, created_by=owner)
    await session.commit()

    user = await invite_service.register_by_invite(
        session, code=invite.code, email="new@example.com", password=PASSWORD
    )
    await session.commit()

    assert user.email == "new@example.com"
    assert invite.used_at is not None
    assert invite.used_by_user_id == user.id


async def test_invite_is_single_use(session, owner):
    invite = await invite_service.create_invite(session, created_by=owner)
    await session.commit()

    await invite_service.register_by_invite(
        session, code=invite.code, email="first@example.com", password=PASSWORD
    )
    await session.commit()

    with pytest.raises(invite_service.InviteAlreadyUsed):
        await invite_service.register_by_invite(
            session, code=invite.code, email="second@example.com", password=PASSWORD
        )


async def test_unknown_code_rejected(session):
    with pytest.raises(invite_service.InviteNotFound):
        await invite_service.register_by_invite(
            session, code="нет-такого", email="a@b.com", password=PASSWORD
        )


async def test_expired_invite_rejected(session, owner):
    invite = await invite_service.create_invite(session, created_by=owner)
    invite.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.commit()

    with pytest.raises(invite_service.InviteExpired):
        await invite_service.register_by_invite(
            session, code=invite.code, email="a@b.com", password=PASSWORD
        )


async def test_revoked_invite_rejected(session, owner):
    invite = await invite_service.create_invite(session, created_by=owner)
    await session.commit()

    await invite_service.revoke_invite(session, invite, by=owner)
    await session.commit()

    with pytest.raises(invite_service.InviteRevoked):
        await invite_service.register_by_invite(
            session, code=invite.code, email="a@b.com", password=PASSWORD
        )


async def test_used_invite_cannot_be_revoked(session, owner):
    invite = await invite_service.create_invite(session, created_by=owner)
    await session.commit()
    await invite_service.register_by_invite(
        session, code=invite.code, email="a@b.com", password=PASSWORD
    )
    await session.commit()

    with pytest.raises(invite_service.InviteAlreadyUsed):
        await invite_service.revoke_invite(session, invite, by=owner)


async def test_invite_bound_to_email(session, owner):
    invite = await invite_service.create_invite(
        session, created_by=owner, email="Expected@Example.com"
    )
    await session.commit()

    with pytest.raises(invite_service.InviteEmailMismatch):
        await invite_service.register_by_invite(
            session, code=invite.code, email="someone-else@example.com", password=PASSWORD
        )

    user = await invite_service.register_by_invite(
        session, code=invite.code, email="expected@example.com", password=PASSWORD
    )
    await session.commit()
    assert user.email == "expected@example.com"


async def test_invite_without_ttl_never_expires(session, owner):
    invite = await invite_service.create_invite(session, created_by=owner, ttl_days=None)
    await session.commit()

    assert invite.expires_at is None
    invite_service.check_usable(invite)


async def test_weak_password_does_not_consume_invite(session, owner):
    """Отказ по паролю не должен сжигать приглашение."""
    invite = await invite_service.create_invite(session, created_by=owner)
    await session.commit()
    code = invite.code

    with pytest.raises(user_service.WeakPassword):
        await invite_service.register_by_invite(
            session, code=code, email="a@b.com", password="short"
        )
    await session.rollback()

    # После отката объекты в сессии протухли — перечитываем из базы.
    refreshed = await invite_service.get_by_code(session, code)
    assert refreshed.used_at is None

    user = await invite_service.register_by_invite(
        session, code=code, email="a@b.com", password=PASSWORD
    )
    await session.commit()
    assert user.id is not None
