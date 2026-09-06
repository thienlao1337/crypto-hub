from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Статусы ключа — результат проверки у биржи, не пользовательский ввод.
KEY_STATUS_PENDING = "pending"
KEY_STATUS_OK = "ok"
KEY_STATUS_INVALID = "invalid"
KEY_STATUS_ERROR = "error"


class Exchange(Base):
    """Справочник бирж — редактируется из админки."""

    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_testnet: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    accounts: Mapped[list["ExchangeAccount"]] = relationship(back_populates="exchange")


class ExchangeAccount(Base):
    """Подключённый API-ключ пользователя к бирже.

    Ключ и секрет хранятся только в зашифрованном виде (Fernet, ключ из
    ENCRYPTION_KEY). В открытом виде не логируются и в шаблоны не
    передаются — в UI показывается api_key_masked.
    """

    __tablename__ = "exchange_accounts"
    __table_args__ = (UniqueConstraint("user_id", "exchange_id", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="main", nullable=False)

    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # Хвост ключа для опознания в интерфейсе, без расшифровки.
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)

    is_testnet: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # requested_trading — чего хотел пользователь при добавлении ключа.
    # allow_trading — что биржа реально подтвердила при проверке прав.
    # Торговля разрешается только когда истинны оба.
    requested_trading: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_trading: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # То же самое для P2P: у обеих бирж эти эндпоинты закрыты, пока
    # аккаунт не получил статус мерчанта или рекламодателя. Права
    # разные, поэтому и флаги отдельные: ключ с правом торговли на споте
    # к объявлениям доступа не даёт.
    requested_p2p: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_p2p: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(32), default=KEY_STATUS_PENDING, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship()  # noqa: F821
    exchange: Mapped["Exchange"] = relationship(back_populates="accounts")

    @property
    def can_trade(self) -> bool:
        return self.allow_trading and self.requested_trading and self.status == KEY_STATUS_OK

    @property
    def can_p2p(self) -> bool:
        return self.allow_p2p and self.requested_p2p and self.status == KEY_STATUS_OK
