"""P2P: объявления, правила ценообразования, журнал и заказы.

P2P — это не биржевой стакан, а доска объявлений: цена стоит там, куда её
поставил человек, и двигается, только когда объявление переписали. Отсюда
и устройство: наши объявления зеркалим у себя, правило считает новую цену
по чужой доске, а каждое изменение попадает в журнал.

Журнал здесь не для порядка, а по необходимости. Бот двигает цену, по
которой у клиента покупают за реальные деньги; вопрос «почему в три часа
ночи мы стояли на процент дешевле рынка» должен иметь ответ в базе, а не
в догадках.
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

# Сторона объявления с нашей точки зрения: мы продаём криптовалюту за
# фиат или покупаем её за фиат.
SIDE_SELL = "sell"
SIDE_BUY = "buy"

# Состояние объявления на бирже.
AD_ONLINE = "online"
AD_OFFLINE = "offline"
AD_CLOSED = "closed"

# Режим правила. observe считает новую цену и пишет в журнал, но
# объявление не трогает: единственный способ посмотреть, что бот
# собирается делать, не платя за это деньгами.
RULE_OBSERVE = "observe"
RULE_LIVE = "live"

EVENT_REPRICED = "repriced"
EVENT_HELD = "held"
EVENT_SKIPPED = "skipped"
EVENT_ERROR = "error"
EVENT_MODE = "mode_changed"


class P2PAd(Base):
    """Наше объявление на площадке — зеркало биржевого.

    Хранится у себя, потому что правило считает изменение относительно
    предыдущего состояния, а спрашивать биржу на каждый чих не даст лимит
    запросов.
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

    # Идентификатор объявления у биржи — по нему мы его и обновляем.
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    # Монета и фиат строками, а не ссылками на assets: фиата в
    # справочнике активов нет и заводить его туда ради P2P значило бы
    # смешать биржевые инструменты с валютами объявлений.
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
    """Как держать цену объявления.

    Пол и потолок обязательны и считаются от спотовой цены. Правило без
    них — это гонка вниз: встретившись с чужим таким же ботом, два
    объявления перебивают друг друга шагами, пока кто-то не разорится.
    Рынок здесь единственная внешняя точка опоры.
    """

    __tablename__ = "p2p_price_rules"
    __table_args__ = (UniqueConstraint("ad_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[int] = mapped_column(
        ForeignKey("p2p_ads.id", ondelete="CASCADE"), nullable=False
    )

    mode: Mapped[str] = mapped_column(String(16), default=RULE_OBSERVE, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Какое место в списке держим. 1 — первое.
    target_position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Насколько перебиваем соседа на этом месте, в валюте объявления.
    step: Mapped[Decimal] = mapped_column(Price, default=Decimal("0.01"), nullable=False)

    # Границы относительно спотовой цены, в процентах. Для продажи
    # floor_pct — минимальная наценка, ceiling_pct — максимальная.
    floor_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)
    ceiling_pct: Mapped[Decimal] = mapped_column(Pct, nullable=False)

    # Соседей мельче или с рейтингом ниже порога не считаем: объявление
    # на пятьдесят долларов от вчерашнего аккаунта утащит цену вниз,
    # ничего при этом не обслуживая.
    min_competitor_amount: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    min_competitor_rate: Mapped[Decimal | None] = mapped_column(Pct, nullable=True)

    # Меньше этого цену не двигаем: каждое обновление — запрос к бирже,
    # и дёргать объявление ради копейки значит выбрать лимит впустую.
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
    """Что правило решило и почему.

    Пишется и когда цену подвинули, и когда решили не двигать: «почему
    бот ничего не сделал» — вопрос не менее частый, чем «почему сделал».
    """

    __tablename__ = "p2p_price_events"
    __table_args__ = (Index("ix_p2p_price_events_ad_time", "ad_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    ad_id: Mapped[int] = mapped_column(ForeignKey("p2p_ads.id", ondelete="CASCADE"), nullable=False)

    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    price_before: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    price_after: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    # Цена соседа, относительно которого принято решение, и спот на тот
    # момент: без них запись в журнале ничего не объясняет.
    competitor_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    spot_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class P2POrder(Base):
    """Заказ по нашему объявлению.

    Нужен для отпуска средств: пока это только зеркало состояния на
    бирже, без автоматических действий.
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
    # Кто и на каком основании отпустил средства. Для ручного отпуска
    # остаётся пустым — его видно и так, по отсутствию записи.
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
