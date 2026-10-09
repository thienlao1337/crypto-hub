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
from app.models.types import Amount, BigPk, BigUsd, Pct, Price, Usd

MARKET_TYPE_SPOT = "spot"
MARKET_TYPE_SWAP = "swap"


class Asset(Base):
    """A coin/token on its own, not tied to a pair or exchange."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Needed to pull market cap and icons from CoinGecko.
    coingecko_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    icon_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Market(Base):
    """A trading pair on a specific exchange.

    The same pair on Bybit and Binance is two separate rows: price, spread and lot
    parameters differ, and the exchange comparison from the spec is built exactly on
    that.
    """

    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("exchange_id", "symbol", "market_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    base_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    quote_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)

    # symbol is the unified ccxt form (BTC/USDT), raw_symbol is the exchange's
    # own (BTCUSDT). The first is for logic, the second for raw WS
    # subscriptions.
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market_type: Mapped[str] = mapped_column(String(16), default=MARKET_TYPE_SPOT, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    price_precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_amount: Mapped[Decimal | None] = mapped_column(Amount, nullable=True)
    tick_size: Mapped[Decimal | None] = mapped_column(Price, nullable=True)

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    exchange: Mapped["Exchange"] = relationship()  # noqa: F821
    base_asset: Mapped["Asset"] = relationship(foreign_keys=[base_asset_id])
    quote_asset: Mapped["Asset"] = relationship(foreign_keys=[quote_asset_id])


class Timeframe(Base):
    """Timeframe reference table - edited from the admin panel."""

    __tablename__ = "timeframes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Candle(Base):
    """An OHLCV candle.

    Stored only for pairs from watchlists and signal rules - otherwise the table grows
    without bound. Old candles are cleaned up by the retention job.
    """

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("market_id", "timeframe_id", "open_time"),
        Index("ix_candles_lookup", "market_id", "timeframe_id", "open_time"),
    )

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), nullable=False)
    timeframe_id: Mapped[int] = mapped_column(ForeignKey("timeframes.id"), nullable=False)

    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Price, nullable=False)
    high: Mapped[Decimal] = mapped_column(Price, nullable=False)
    low: Mapped[Decimal] = mapped_column(Price, nullable=False)
    close: Mapped[Decimal] = mapped_column(Price, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Amount, nullable=False)

    # An open candle is updated on every poll, a closed one never again.
    is_closed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class MarketTicker(Base):
    """Latest market state. One row per pair, overwritten.

    The bot and the alert engine read it so they don't hit the exchange on every
    request. Price history lives in candles; this is only "now".
    """

    __tablename__ = "market_tickers"

    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), primary_key=True
    )
    last: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    high_24h: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    low_24h: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    volume_24h: Mapped[Decimal | None] = mapped_column(Amount, nullable=True)
    # Turnover in the quote currency. Sorting markets by volume in coins is
    # meaningless: memecoin amounts are measured in trillions and push
    # everything else down the list.
    quote_volume_24h: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)
    change_24h_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    market: Mapped["Market"] = relationship()


class GlobalStats(Base):
    """Snapshot of market-wide metrics for dashboard widgets.

    Stored as history, not a single row: the Fear & Greed chart needs the trend, and the
    source only returns the current value.
    """

    __tablename__ = "global_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    total_market_cap_usd: Mapped[Decimal | None] = mapped_column(BigUsd, nullable=True)
    total_volume_24h_usd: Mapped[Decimal | None] = mapped_column(BigUsd, nullable=True)
    market_cap_change_24h_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)
    btc_dominance: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)
    eth_dominance: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    # Fear & Greed index: 0..100 plus a text label from the source.
    fng_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fng_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
