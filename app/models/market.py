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
from app.models.types import Amount, BigPk, Pct, Price, Usd

MARKET_TYPE_SPOT = "spot"
MARKET_TYPE_SWAP = "swap"


class Asset(Base):
    """Монета/токен сам по себе, вне привязки к паре и бирже."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Нужен для подтягивания капитализации и иконок из CoinGecko.
    coingecko_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    icon_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Market(Base):
    """Торговая пара на конкретной бирже.

    Одна и та же пара на Bybit и Binance — две разные записи: у них
    расходятся цена, спред и параметры лота, и сравнение бирж из ТЗ
    строится именно на этом.
    """

    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("exchange_id", "symbol", "market_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    base_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    quote_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)

    # symbol — унифицированный вид ccxt (BTC/USDT), raw_symbol — как у
    # биржи (BTCUSDT). Первый для логики, второй для сырых WS-подписок.
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
    """Справочник таймфреймов — редактируется из админки."""

    __tablename__ = "timeframes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Candle(Base):
    """Свеча OHLCV.

    Храним только по парам из watchlist и правил сигналов — иначе таблица
    растёт неограниченно. Старые свечи подчищает задача ретеншна.
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

    # Незакрытая свеча обновляется на каждом опросе, закрытая — больше нет.
    is_closed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class MarketTicker(Base):
    """Последнее состояние рынка. Одна строка на пару, перезаписывается.

    Сюда смотрят бот и движок алертов, чтобы не дёргать биржу на каждый
    запрос. История цен живёт в candles, здесь только «сейчас».
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
    change_24h_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    market: Mapped["Market"] = relationship()


class GlobalStats(Base):
    """Снимок общерыночных показателей для виджетов дашборда.

    Храним историей, а не одной строкой: на графике индекса страха и
    жадности нужна динамика, а источник отдаёт только текущее значение.
    """

    __tablename__ = "global_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    total_market_cap_usd: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)
    total_volume_24h_usd: Mapped[Decimal | None] = mapped_column(Usd, nullable=True)
    market_cap_change_24h_pct: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)
    btc_dominance: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)
    eth_dominance: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    # Индекс страха и жадности: 0..100 + текстовая метка от источника.
    fng_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fng_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
