"""Слой D: доставка.

notifications — она же очередь: воркер разбирает pending через
FOR UPDATE SKIP LOCKED. Отдельной таблицы-очереди нет намеренно.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('web', 'email', 'trueconf')", name="notifications_channel_chk"
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="notifications_status_chk",
        ),
        # Один инцидент — одно уведомление на канал пользователю, даже если он
        # подписан на несколько подходящих тем (FR-DLV-2)
        UniqueConstraint("incident_id", "user_id", "channel", name="notifications_uq"),
        # Очередь доставки
        Index(
            "notifications_pending_idx",
            "created_at",
            postgresql_where=text("status = 'pending'::text"),
        ),
        Index(
            "notifications_retry_idx",
            "next_retry_at",
            postgresql_where=text("status = 'pending'::text AND next_retry_at IS NOT NULL"),
        ),
        # Инбокс
        Index(
            "notifications_inbox_idx",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("channel = 'web'::text"),
        ),
        Index(
            "notifications_unread_idx",
            "user_id",
            postgresql_where=text("channel = 'web'::text AND read_at IS NULL"),
        ),
        {
            "comment": (
                "Экземпляр доставки. Адрес получателя здесь НЕ хранится — резолвится "
                "ContactResolver'ом в момент отправки и нигде не персистится (FR-DLV-4)."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    topic_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("topics.id", ondelete="SET NULL"),
        comment="По какой теме прилетело",
    )

    channel: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'pending'"),
        comment=(
            "skipped — подписка была заглушена или сработало окно подавления; строка "
            "сохраняется, чтобы было видно, что решение принято осознанно."
        ),
    )

    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Только для канала web"
    )


class LlmSummary(Base):
    __tablename__ = "llm_summaries"
    __table_args__ = (
        UniqueConstraint("incident_id", "prompt_version", name="llm_summaries_uq"),
        {
            "comment": (
                "Модель и версия промпта фиксируются, чтобы результаты были воспроизводимы "
                "и сравнимы между прогонами. Недоступность модели не блокирует доставку."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )

    model: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Имя модели на локальном endpoint"
    )
    prompt_version: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Версия шаблона промпта"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Человекочитаемая сводка"
    )
    masked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="Применялось ли маскирование",
    )

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
