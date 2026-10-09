from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.types import BigPk, JsonB


class AuditLog(Base):
    """User actions on significant entities.

    Separate from login_events: that one is about signing in, this one about what was
    done inside - an exchange key added, trading permissions enabled, a strategy
    switched to live. Such things must be reconstructable after the fact.
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
    # Never contains secrets: only the fact and safe details.
    payload: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User | None"] = relationship()  # noqa: F821
