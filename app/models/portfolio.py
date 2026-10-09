from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import Amount, BigPk, JsonB, Price, Usd

SIDE_BUY = "buy"
SIDE_SELL = "sell"


class Balance(Base):
    """Current balance of a coin on one connected exchange account.

    Overwritten on every sync: it's a "now" snapshot. Portfolio value history is kept in
    portfolio_snapshots.
    """

    __tablename__ = "balances"
    __table_args__ = (UniqueConstraint("exchange_account_id", "asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)

    free: Mapped[Decimal] = mapped_column(Amount, default=0, nullable=False)
    locked: Mapped[Decimal] = mapped_column(Amount, default=0, nullable=False)
    total: Mapped[Decimal] = mapped_column(Amount, default=0, nullable=False)
    usd_value: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    exchange_account: Mapped["ExchangeAccount"] = relationship()  # noqa: F821
    asset: Mapped["Asset"] = relationship()  # noqa: F821


class PortfolioSnapshot(Base):
    """A point on the portfolio value chart.

    breakdown stores the split by exchange and coin at the time of the snapshot, so last
    month's chart isn't recomputed retroactively at today's prices.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_portfolio_snapshots_user_time", "user_id", "captured_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    total_usd: Mapped[Decimal] = mapped_column(Usd, nullable=False)
    breakdown: Mapped[dict | None] = mapped_column(JsonB, nullable=True)


class Trade(Base):
    """An executed trade pulled from the exchange history.

    external_id is the exchange's trade id; uniqueness on it together with the account
    makes re-syncing idempotent.
    """

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("exchange_account_id", "external_id"),
        Index("ix_trades_account_time", "exchange_account_id", "executed_at"),
    )

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    exchange_account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)

    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Amount, nullable=False)
    cost: Mapped[Decimal] = mapped_column(Usd, nullable=False)

    fee: Mapped[Decimal | None] = mapped_column(Amount, nullable=True)
    fee_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    # Filled only when the trade closes a position.
    realized_pnl: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)

    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Raw exchange response - in case we need to investigate mismatched numbers.
    raw: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    market: Mapped["Market"] = relationship()  # noqa: F821
    fee_asset: Mapped["Asset | None"] = relationship(foreign_keys=[fee_asset_id])  # noqa: F821


class Position(Base):
    """An open position with unrealized PnL.

    For derivatives it comes from the exchange as is. For spot it is built from trades:
    average entry price against the current quote.
    """

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("exchange_account_id", "market_id", "side"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)

    side: Mapped[str] = mapped_column(String(8), default=SIDE_BUY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Amount, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)
    leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The average entry price is reliable only if the full trade history was
    # pulled. Exchanges return a limited period, so this flag honestly tells
    # the UI to show the number with a caveat.
    cost_basis_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    market: Mapped["Market"] = relationship()  # noqa: F821


class WatchlistItem(Base):
    """A pair in the personal watchlist."""

    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "market_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    market: Mapped["Market"] = relationship()  # noqa: F821
