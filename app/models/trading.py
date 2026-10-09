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

# paper - trades only in the database, the exchange isn't touched at all.
# testnet - real calls to the exchange's test network.
# live - real money; enabled by a separate confirmation.
MODE_PAPER = "paper"
MODE_TESTNET = "testnet"
MODE_LIVE = "live"


class Strategy(Base):
    """Auto-trading strategy: signal -> position size -> SL/TP.

    Inactive and in paper mode by default. Switching to live requires a recorded time of
    explicit confirmation (live_confirmed_at) - a single checkbox isn't enough for real
    money.
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

    # --- Position size and exits ---
    position_size_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    stop_loss_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)
    take_profit_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    # --- Risk limits ---
    # When the daily loss limit is reached, the strategy stops itself and logs
    # the reason.
    max_pct_per_trade: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    daily_loss_limit_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Marker for "signals up to this id have already been reviewed". Without it
    # every background pass re-processes the same signals and logs the same
    # rejections - thirty identical lines pile up in half an hour, and the one
    # that matters gets lost among them. This is a watermark, not a reference,
    # hence no foreign key: deleting an old signal must not make the strategy
    # reconsider everything.
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
    """An order placed by the bot. mode is duplicated here on purpose:

    the strategy mode may change later, and the log must show what exactly the trade was
    at the moment of execution.
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
    # Exit price and result are filled in when the position closes. One row
    # covers the whole position: entry and exit as two rows would have to be
    # stitched back together every time they're shown.
    close_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    strategy: Mapped["Strategy"] = relationship()


class BotJournalEntry(Base):
    """Bot action log - full transparency, per the spec.

    We record everything: reviewed a signal and declined, placed an order, hit an
    exchange error, stopped at the loss limit.
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
    """Daily risk state of a strategy.

    One row per trading day: accumulated result and a stop flag.
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
