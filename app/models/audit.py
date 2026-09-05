from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import BigPk, JsonB


class AuditLog(Base):
    """Действия пользователя над значимыми сущностями.

    Отдельно от login_events: там про вход, здесь про то, что делали
    внутри — добавили ключ биржи, включили торговые права, перевели
    стратегию в live. Такие вещи должны быть восстановимы постфактум.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigPk, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Никогда не содержит секретов: только факт и безопасные детали.
    payload: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User | None"] = relationship()  # noqa: F821
