from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import BigPk, JsonB, Price

CHANNEL_WEB = "web"
CHANNEL_TELEGRAM = "telegram"

NOTIFICATION_ALERT = "alert"
NOTIFICATION_SIGNAL = "signal"
NOTIFICATION_SYSTEM = "system"


class AlertType(Base):
    """Alert type reference table - edited from the admin panel.

    code is interpreted by the check engine: price_above, price_below, pct_change, rsi.
    """

    __tablename__ = "alert_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Alert(Base):
    """A user alert.

    params depends on the type: {"level": "70000"} for price,
    {"pct": 5, "window_minutes": 60} for change,
    {"period": 14, "threshold": 70, "direction": "above"} for RSI.
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    alert_type_id: Mapped[int] = mapped_column(ForeignKey("alert_types.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), nullable=False)

    params: Mapped[dict] = mapped_column(JsonB, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Cooldown after triggering - otherwise a "price above X" alert would fire
    # on every check while the price stays above the level.
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # NULL - trigger an unlimited number of times.
    trigger_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notify_web: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    alert_type: Mapped["AlertType"] = relationship()
    market: Mapped["Market"] = relationship()  # noqa: F821
    triggers: Mapped[list["AlertTrigger"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )


class AlertTrigger(Base):
    """A trigger event - a separate row, not an overwritten counter."""

    __tablename__ = "alert_triggers"
    __table_args__ = (Index("ix_alert_triggers_time", "alert_id", "triggered_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False)

    price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    delivered_web: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivered_telegram: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    alert: Mapped["Alert"] = relationship(back_populates="triggers")


class Notification(Base):
    """An entry in the web panel notification feed."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Whether to show the entry in the panel feed. A notification limited to
    # Telegram still goes through this table - it is also the send queue - but
    # doesn't appear in the feed.
    show_web: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Sending to Telegram is separate from recording: the notification isn't
    # lost if the bot is unavailable at that moment, and goes out on the next
    # pass.
    delivered_telegram: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Web push follows the same channel as the feed: push is a way to deliver
    # to the browser what would have landed in the feed anyway.
    delivered_push: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationSetting(Base):
    """What to send and where. A missing row is treated as "enabled"."""

    __tablename__ = "notification_settings"
    __table_args__ = (UniqueConstraint("user_id", "event_type", "channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PushSubscription(Base):
    """A browser web-push subscription.

    One row per device and browser: subscribed from a phone and a laptop, the user gets
    the notification on both. endpoint is issued by the browser's push service and
    doubles as the identifier - resubscribing from the same device updates the row
    instead of adding new ones.
    """

    __tablename__ = "push_subscriptions"
    __table_args__ = (UniqueConstraint("endpoint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    # Keys from the browser's PushSubscription: they encrypt the payload, which
    # only this device can read.
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)

    # So the device list shows which one is which.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
