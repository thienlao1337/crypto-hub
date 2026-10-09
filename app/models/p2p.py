"""P2P: ads, pricing rules, the log and orders.

P2P is not an exchange order book but an ad board: a price stays where a person put it
and only moves when the ad is rewritten. Hence the design: we mirror our ads locally,
the rule computes a new price from other people's ads, and every change goes into the
log.

The log here isn't for tidiness but out of necessity. The bot moves a price at which
people buy from the client for real money; the question "why were we one percent below
market at three in the morning" must have an answer in the database, not in guesswork.
"""

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
from app.models.types import Amount, BigPk, JsonB, Price, Pct

# Ad side from our point of view: we sell crypto for fiat or buy it with fiat.
SIDE_SELL = "sell"
SIDE_BUY = "buy"

# Ad state on the exchange.
AD_ONLINE = "online"
AD_OFFLINE = "offline"
AD_CLOSED = "closed"

# Rule mode. observe computes the new price and logs it but leaves the ad
# alone: the only way to see what the bot is about to do without paying for it
# with real money.
RULE_OBSERVE = "observe"
RULE_LIVE = "live"

EVENT_REPRICED = "repriced"
EVENT_HELD = "held"
EVENT_SKIPPED = "skipped"
EVENT_ERROR = "error"
EVENT_MODE = "mode_changed"


class P2PAd(Base):
    """Our ad on the marketplace - a mirror of the exchange one.

    Stored locally because the rule computes changes relative to the previous state, and
    the request limit won't allow asking the exchange about every little thing.
    """

    __tablename__ = "p2p_ads"
    __table_args__ = (
        UniqueConstraint("exchange_account_id", "external_id"),
        Index("ix_p2p_ads_account", "exchange_account_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )

    # Ad id on the exchange - that's how we update it.
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    # Coin and fiat as strings, not references to assets: there's no fiat in
    # the asset reference table, and adding it there for P2P would mix exchange
    # instruments with ad currencies.
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    fiat: Mapped[str] = mapped_column(String(8), nullable=False)

    price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Amount, nullable=True)
    min_amount: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    max_amount: Mapped[Decimal | None] = mapped_column(Price, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default=AD_OFFLINE, nullable=False)
    payment_methods: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped["ExchangeAccount"] = relationship()  # noqa: F821
    rule: Mapped["P2PPriceRule | None"] = relationship(back_populates="ad", uselist=False)


class P2PPriceRule(Base):
    """How to hold the ad price.

    Floor and ceiling are mandatory and are computed from the spot price. A rule without
    them is a race to the bottom: when it meets someone else's similar bot, the two ads
    keep outbidding each other step by step until one of them goes broke. The market is
    the only external anchor here.
    """

    __tablename__ = "p2p_price_rules"
    __table_args__ = (UniqueConstraint("ad_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[int] = mapped_column(
        ForeignKey("p2p_ads.id", ondelete="CASCADE"), nullable=False
    )

    mode: Mapped[str] = mapped_column(String(16), default=RULE_OBSERVE, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Which position in the list we hold. 1 is the first.
    target_position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # How much we outbid the neighbour at that position, in the ad currency.
    step: Mapped[Decimal] = mapped_column(Price, default=Decimal("0.01"), nullable=False)

    # Bounds relative to the spot price, in percent. For selling, floor_pct is
    # the minimum markup and ceiling_pct the maximum.
    floor_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    ceiling_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)

    # Neighbours that are smaller or rated below the threshold are ignored: a
    # fifty-dollar ad from yesterday's account would drag the price down while
    # serving nobody.
    min_competitor_amount: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    min_competitor_rate: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    # We don't move the price by less than this: every update is an exchange
    # request, and nudging the ad for pennies wastes the rate limit.
    min_change: Mapped[Decimal] = mapped_column(Price, default=Decimal("0.01"), nullable=False)

    last_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ad: Mapped["P2PAd"] = relationship(back_populates="rule")


class P2PPriceEvent(Base):
    """What the rule decided and why.

    Written both when the price was moved and when it was left alone: "why did the bot
    do nothing" is asked just as often as "why did it do that".
    """

    __tablename__ = "p2p_price_events"
    __table_args__ = (Index("ix_p2p_price_events_ad_time", "ad_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    ad_id: Mapped[int] = mapped_column(ForeignKey("p2p_ads.id", ondelete="CASCADE"), nullable=False)

    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    price_before: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    price_after: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    # The neighbour's price the decision was based on, and the spot price at
    # that moment: without them the log entry explains nothing.
    competitor_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    spot_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class P2POrder(Base):
    """An order on our ad.

    Needed for releasing funds: for now it only mirrors the state on the exchange, with
    no automatic actions.
    """

    __tablename__ = "p2p_orders"
    __table_args__ = (
        UniqueConstraint("exchange_account_id", "external_id"),
        Index("ix_p2p_orders_account_time", "exchange_account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    exchange_account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    ad_id: Mapped[int | None] = mapped_column(
        ForeignKey("p2p_ads.id", ondelete="SET NULL"), nullable=True
    )

    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    fiat: Mapped[str] = mapped_column(String(8), nullable=False)

    amount: Mapped[Decimal | None] = mapped_column(Amount, nullable=True)
    fiat_amount: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String(128), nullable=True)

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Who released the funds and on what grounds. Left empty for a manual
    # release - that's visible anyway from the missing record.
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
