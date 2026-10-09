import pytest_asyncio
from sqlalchemy import select

from app.models import Notification
from app.services import notification_service as ns
from app.services import user_service


@pytest_asyncio.fixture
async def user(session):
    person = await user_service.create_user(
        session, email="reader@example.com", password="reader-password-1"
    )
    await session.commit()
    return person


async def dispatch(session, user, **kwargs):
    return await ns.dispatch(
        session,
        user_id=user.id,
        kind=kwargs.pop("kind", ns.KIND_ALERT),
        title="Заголовок",
        body="Текст",
        **kwargs,
    )


# --- Defaults ---


async def test_untouched_settings_mean_enabled(session, user):
    """A user who hasn't configured anything gets notifications."""
    notification = await dispatch(session, user)
    await session.commit()

    assert notification is not None
    assert notification.show_web is True
    assert notification.delivered_telegram is False


async def test_settings_matrix_defaults_to_all_on(session, user):
    matrix = await ns.settings_matrix(session, user)

    assert set(matrix) == {
        (event, channel) for event in ns.EVENT_KINDS for channel in ns.CHANNELS
    }
    assert all(matrix.values())


# --- Settings narrow the channels ---


async def test_disabled_web_hides_from_feed_but_still_sends(session, user):
    await ns.set_enabled(session, user.id, ns.KIND_ALERT, ns.CHANNEL_WEB, False)
    await session.commit()

    notification = await dispatch(session, user)
    await session.commit()

    assert notification is not None
    assert notification.show_web is False
    # Telegram stayed enabled, so the row is waiting to be sent.
    assert notification.delivered_telegram is False
    assert await ns.recent(session, user) == []
    assert await ns.unread_count(session, user) == 0


async def test_disabled_telegram_marks_delivered_immediately(session, user):
    """A disabled channel must not linger in the send queue."""
    await ns.set_enabled(session, user.id, ns.KIND_ALERT, ns.CHANNEL_TELEGRAM, False)
    await session.commit()

    notification = await dispatch(session, user)
    await session.commit()

    assert notification.show_web is True
    assert notification.delivered_telegram is True


async def test_both_channels_off_writes_nothing(session, user):
    for channel in ns.CHANNELS:
        await ns.set_enabled(session, user.id, ns.KIND_ALERT, channel, False)
    await session.commit()

    assert await dispatch(session, user) is None
    await session.commit()

    assert (await session.execute(select(Notification))).scalars().all() == []


async def test_settings_apply_per_event_kind(session, user):
    """Disabled signals must not mute alerts."""
    for channel in ns.CHANNELS:
        await ns.set_enabled(session, user.id, ns.KIND_SIGNAL, channel, False)
    await session.commit()

    assert await dispatch(session, user, kind=ns.KIND_SIGNAL) is None
    assert await dispatch(session, user, kind=ns.KIND_ALERT) is not None


async def test_source_flags_cannot_widen_settings(session, user):
    """A single alert's checkbox doesn't enable a channel disabled in the settings."""
    await ns.set_enabled(session, user.id, ns.KIND_ALERT, ns.CHANNEL_TELEGRAM, False)
    await session.commit()

    notification = await dispatch(session, user, telegram=True)
    await session.commit()

    assert notification.delivered_telegram is True


async def test_source_flags_can_narrow_settings(session, user):
    """But it can disable a channel for one alert."""
    notification = await dispatch(session, user, web=False)
    await session.commit()

    assert notification.show_web is False


# --- Feed ---


async def test_mark_all_read_ignores_hidden_rows(session, user):
    await ns.push(session, user_id=user.id, kind=ns.KIND_ALERT, title="t", body="b")
    await ns.push(
        session, user_id=user.id, kind=ns.KIND_ALERT, title="t", body="b", show_web=False
    )
    await session.commit()

    assert await ns.mark_all_read(session, user) == 1


async def test_set_enabled_updates_existing_row(session, user):
    await ns.set_enabled(session, user.id, ns.KIND_ALERT, ns.CHANNEL_WEB, False)
    await ns.set_enabled(session, user.id, ns.KIND_ALERT, ns.CHANNEL_WEB, True)
    await session.commit()

    matrix = await ns.settings_matrix(session, user)
    assert matrix[(ns.KIND_ALERT, ns.CHANNEL_WEB)] is True
