from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import Amount, BigPk, JsonB, Pct, Price, Usd

# paper — сделки только в базе, биржа не дёргается вообще.
# testnet — реальные вызовы к тестовой сети биржи.
# live — реальные деньги; включается отдельным подтверждением.
MODE_PAPER = "paper"
MODE_TESTNET = "testnet"
MODE_LIVE = "live"


class Strategy(Base):
    """Стратегия автотрейдинга: сигнал → размер позиции → SL/TP.

    По умолчанию неактивна и в режиме paper. Перевод в live требует
    записанного времени явного подтверждения (live_confirmed_at) — одного
    чекбокса для реальных денег мало.
    """

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    signal_rule_id: Mapped[int] = mapped_column(ForeignKey("signal_rules.id"), nullable=False)
    exchange_account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)

    mode: Mapped[str] = mapped_column(String(16), default=MODE_PAPER, nullable=False)

    # --- Размер позиции и выходы ---
    position_size_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    stop_loss_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)
    take_profit_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    # --- Лимиты риска ---
    # При достижении дневного лимита убытка стратегия останавливает сама
    # себя и пишет причину в журнал.
    max_pct_per_trade: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    daily_loss_limit_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Отметка «сигналы до этого номера уже рассмотрены». Без неё каждый
    # проход фонового процесса заново разбирает те же сигналы и пишет в
    # журнал те же отказы — за полчаса набегает три десятка одинаковых
    # строк, и единственная важная теряется среди них. Это водяной знак,
    # а не ссылка, поэтому без внешнего ключа: удаление старого сигнала
    # не должно заставлять стратегию всё переосмысливать.
    last_signal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    live_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    signal_rule: Mapped["SignalRule"] = relationship()  # noqa: F821
    market: Mapped["Market"] = relationship()  # noqa: F821
    exchange_account: Mapped["ExchangeAccount"] = relationship()  # noqa: F821

    @property
    def is_live(self) -> bool:
        return self.mode == MODE_LIVE and self.live_confirmed_at is not None


class BotOrder(Base):
    """Ордер, выставленный ботом. mode дублируется здесь намеренно:

    режим стратегии может измениться позже, а по журналу должно быть
    видно, чем именно была сделка в момент исполнения.
    """

    __tablename__ = "bot_orders"
    __table_args__ = (Index("ix_bot_orders_strategy_time", "strategy_id", "opened_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    exchange_account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)

    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Amount, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    stop_loss: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    # Цена выхода и результат заполняются при закрытии позиции. Строка
    # одна на позицию целиком: вход и выход двумя записями пришлось бы
    # сшивать обратно при каждом показе.
    close_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    strategy: Mapped["Strategy"] = relationship()


class BotJournalEntry(Base):
    """Журнал действий бота — по ТЗ полная прозрачность.

    Пишем всё: рассмотрел сигнал и отказался, выставил ордер, поймал
    ошибку биржи, остановился по лимиту убытка.
    """

    __tablename__ = "bot_journal"
    __table_args__ = (Index("ix_bot_journal_strategy_time", "strategy_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)

    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskState(Base):
    """Дневное состояние риска по стратегии.

    Одна строка на торговый день: накопленный результат и флаг остановки.
    """

    __tablename__ = "risk_state"
    __table_args__ = (UniqueConstraint("strategy_id", "trading_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)

    realized_pnl_pct: Mapped[Decimal] = mapped_column(Pct, default=0, nullable=False)
    trades_count: Mapped[int] = mapped_column(default=0, nullable=False)

    is_halted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
