"""Слой E: служебные справочники и аудит."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SeverityMap(Base):
    """Шкала критичности в таблице, а не в коде: источники будут добавляться."""

    __tablename__ = "severity_map"
    __table_args__ = (
        PrimaryKeyConstraint("source", "raw_value", name="severity_map_pk"),
        ForeignKeyConstraint(
            ["source"],
            ["event_sources.code"],
            name="severity_map_fk",
            ondelete="CASCADE",
        ),
        CheckConstraint("severity BETWEEN 0 AND 5", name="severity_map_chk"),
    )

    source: Mapped[str] = mapped_column(Text, nullable=False)
    raw_value: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Как пишет источник, регистронезависимо"
    )
    severity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("audit_log_created_idx", text("created_at DESC")),
        Index("audit_log_actor_idx", "actor_id", text("created_at DESC")),
        Index("audit_log_entity_idx", "entity_type", "entity_id"),
        {
            "comment": (
                "Не писать сюда адреса доставки, тела уведомлений и payload событий — "
                "журнал не должен становиться обходным хранилищем ПДн."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), comment="NULL = системное действие"
    )
    action: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="topic.create | subscription.assign | user.login ...",
    )
    entity_type: Mapped[str] = mapped_column(
        Text, nullable=False, comment="topic | subscription | user | role"
    )
    entity_id: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
