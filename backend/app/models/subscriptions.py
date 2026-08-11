"""Слой C: каталог тем и подписки.

Обязательные подписки НЕ материализуются строками: они вычисляются из
topic_roles + user_roles. Эффективный набор отдаёт вьюха
v_effective_subscriptions (создаётся миграцией, ORM её не выражает).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Topic(Base):
    """Тема — единица подписки. Вся сложность отбора событий спрятана в match_rule."""

    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("code", name="topics_code_uq"),
        Index("topics_active_idx", "is_active", postgresql_where=text("is_active")),
        Index(
            "topics_rule_gin_idx",
            "match_rule",
            postgresql_using="gin",
            postgresql_ops={"match_rule": "jsonb_path_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False, comment="«БД prod», «Сеть МСК»")
    description: Mapped[str | None] = mapped_column(Text)

    match_rule: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'"),
        comment=(
            "Условия соединяются логическим И. Внутри списка — ИЛИ. Пустой объект ловит "
            "все события. Проверяется кодом маршрутизатора, не триггером БД."
        ),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TopicRole(Base):
    """Привязка темы к роли делает подписку обязательной для носителей роли."""

    __tablename__ = "topic_roles"
    __table_args__ = (
        PrimaryKeyConstraint("topic_id", "role_code", name="topic_roles_pk"),
        {
            "comment": (
                "Обязательные подписки НЕ материализуются строками в subscriptions — они "
                "вычисляются из этой таблицы и user_roles. Иначе пришлось бы пересчитывать "
                "подписки при каждом изменении роли."
            )
        },
    )

    topic_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(
        Text, ForeignKey("roles.code", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Subscription(Base):
    """Явные подписки: назначенная администратором и добровольная."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("origin IN ('assigned', 'self')", name="subscriptions_origin_chk"),
        UniqueConstraint("user_id", "topic_id", name="subscriptions_uq"),
        # Назначенная подписка обязана знать, кто её назначил
        CheckConstraint(
            "origin <> 'assigned' OR granted_by IS NOT NULL",
            name="subscriptions_granted_chk",
        ),
        Index("subscriptions_topic_idx", "topic_id"),
        Index("subscriptions_user_idx", "user_id"),
        {
            "comment": (
                "Одна строка на пару (пользователь, тема). origin=assigned снимает только "
                "администратор, origin=self — сам пользователь. Обязательных подписок "
                "здесь нет."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    topic_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )

    origin: Mapped[str] = mapped_column(Text, nullable=False)
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{web}'"),
        comment="web | email | trueconf",
    )

    granted_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), comment="Кто назначил (для assigned)"
    )
    muted_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Временное глушение"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
