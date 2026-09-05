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
    """Правило, по которому считаются сигналы.

    config держит параметры индикаторов словарём, а не колонками: набор
    индикаторов по ТЗ должен расширяться без миграции схемы.
    Например: {"ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
               "rsi_overbought": 70, "rsi_oversold": 30}
    """

    __tablename__ = "signal_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL — правило по умолчанию, общее для всех пользователей.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL — применять ко всем парам из watchlist пользователя.
    market_id: Mapped[int | None] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=True
    )
    timeframe_id: Mapped[int] = mapped_column(ForeignKey("timeframes.id"), nullable=False)

    config: Mapped[dict] = mapped_column(JsonB, nullable=False)

    # Через сколько минут проверять, «сыграл» ли сигнал — основа
    # статистики точности.
    evaluation_horizon_minutes: Mapped[int] = mapped_column(Integer, default=1440, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    market: Mapped["Market | None"] = relationship()  # noqa: F821
    timeframe: Mapped["Timeframe"] = relationship()  # noqa: F821


class Signal(Base):
    """Сработавший сигнал.

    reason — человеческое обоснование («EMA9 пересекла EMA21 снизу вверх,
    RSI 58 — не перекуплен»), indicators — значения на момент срабатывания.
    По ТЗ карточка сигнала обязана объяснять, почему он появился.
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

    # Время свечи, на которой сработало правило: защищает от повторной
    # выдачи того же сигнала при следующем проходе.
    candle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rule: Mapped["SignalRule"] = relationship()
    market: Mapped["Market"] = relationship()  # noqa: F821
    outcome: Mapped["SignalOutcome | None"] = relationship(back_populates="signal")


class SignalOutcome(Base):
    """Что стало с ценой через горизонт проверки — материал для статистики."""

    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    horizon_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_after: Mapped[Decimal] = mapped_column(Price, nullable=False)
    pnl_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    # «Сыграл» — цена ушла в сторону сигнала. Нейтральные не оцениваем.
    is_success: Mapped[bool] = mapped_column(Boolean, nullable=False)

    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    signal: Mapped["Signal"] = relationship(back_populates="outcome")
