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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import BigPk, JsonB, Pct, Price

DIRECTION_BUY = "buy"
DIRECTION_SELL = "sell"
DIRECTION_NEUTRAL = "neutral"


class SignalRule(Base):
    """The rule signals are computed by.

    config holds indicator parameters as a dict rather than columns: per the spec, the
    indicator set must grow without schema migrations.
    For example: {"ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
                  "rsi_overbought": 70, "rsi_oversold": 30}
    """

    __tablename__ = "signal_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL - the default rule, shared by all users.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL - apply to all pairs in the user's watchlist.
    market_id: Mapped[int | None] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=True
    )
    timeframe_id: Mapped[int] = mapped_column(ForeignKey("timeframes.id"), nullable=False)

    config: Mapped[dict] = mapped_column(JsonB, nullable=False)

    # After how many minutes to check whether the signal "played out" - the
    # basis of accuracy statistics.
    evaluation_horizon_minutes: Mapped[int] = mapped_column(Integer, default=1440, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    market: Mapped["Market | None"] = relationship()  # noqa: F821
    timeframe: Mapped["Timeframe"] = relationship()  # noqa: F821


class Signal(Base):
    """A fired signal.

    reason is a human-readable justification ("EMA9 crossed EMA21 upwards, RSI 58 - not
    overbought"), indicators are the values at the moment it fired. Per the spec, a
    signal card must explain why it appeared.
    """

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_market_time", "market_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("signal_rules.id", ondelete="CASCADE"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), nullable=False)
    timeframe_id: Mapped[int] = mapped_column(ForeignKey("timeframes.id"), nullable=False)

    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    indicators: Mapped[dict] = mapped_column(JsonB, nullable=False)

    # Time of the candle the rule fired on: protects against issuing the same
    # signal again on the next pass.
    candle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rule: Mapped["SignalRule"] = relationship()
    market: Mapped["Market"] = relationship()  # noqa: F821
    outcome: Mapped["SignalOutcome | None"] = relationship(back_populates="signal")


class SignalOutcome(Base):
    """What happened to the price after the check horizon - material for statistics."""

    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    horizon_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_after: Mapped[Decimal] = mapped_column(Price, nullable=False)
    pnl_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    # "Played out" - the price moved in the signal's direction. Neutral signals aren't scored.
    is_success: Mapped[bool] = mapped_column(Boolean, nullable=False)

    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    signal: Mapped["Signal"] = relationship(back_populates="outcome")
