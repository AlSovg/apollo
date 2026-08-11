"""Слой B: субъекты и роли.

ПДн-политика (раздел 5 AGENTS.md): ФИО, должность и телефон здесь не хранятся
и добавляться не должны. Отображаемое имя подтягивается из каталога по запросу.
Единственное место с контактами — user_contacts, временная таблица на MVP.
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
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("provider IN ('local', 'ldap')", name="users_provider_chk"),
        UniqueConstraint("provider", "subject_id", name="users_subject_uq"),
        UniqueConstraint("username", name="users_username_uq"),
        # У локального пользователя пароль обязателен, у доменного его быть не должно
        CheckConstraint(
            "(provider = 'local' AND password_hash IS NOT NULL) OR "
            "(provider = 'ldap'  AND password_hash IS NULL)",
            name="users_password_chk",
        ),
        {
            "comment": (
                "Субъекты системы. Осознанно не содержит ПДн: ни ФИО, ни телефона, ни "
                "email. Связка subject_id + подписки остаётся косвенными ПДн, но объём "
                "минимален."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    subject_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "Непрозрачный стабильный идентификатор субъекта. Для LDAP — objectGUID, "
            "для локального провайдера — сгенерированный UUID. НЕ логин и НЕ email."
        ),
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)

    username: Mapped[str] = mapped_column(Text, nullable=False, comment="Логин для входа")
    password_hash: Mapped[str | None] = mapped_column(
        Text, comment="argon2id; NULL для пользователей из каталога"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    failed_logins: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    roles_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="Когда последний раз обновляли роли из каталога",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserContact(Base):
    """ВНИМАНИЕ: единственная таблица схемы, содержащая ПДн."""

    __tablename__ = "user_contacts"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "channel", name="user_contacts_pk"),
        CheckConstraint(
            "channel IN ('email', 'trueconf')", name="user_contacts_channel_chk"
        ),
        {
            "comment": (
                "ВНИМАНИЕ: содержит персональные данные. Временная таблица на период MVP, "
                "пока нет каталога. Не логировать содержимое. Очищается при переключении "
                "на LDAP."
            )
        },
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Session(Base):
    """Refresh-сессия: нужна для ротации токенов и их отзыва.

    Без серверного состояния подписанный refresh-токен невозможно отозвать,
    а FR-IDM-4 требует закрывать доступ немедленно при отключении учётки.
    Хранится только хеш идентификатора токена — самого токена в БД нет.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("jti", name="sessions_jti_uq"),
        Index("sessions_user_idx", "user_id", "revoked_at"),
        {
            "comment": (
                "Активные refresh-сессии. Отзыв — проставление revoked_at: "
                "деактивация учётки закрывает доступ сразу, не дожидаясь истечения токена."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    jti: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Идентификатор refresh-токена"
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[str | None] = mapped_column(
        Text, comment="jti токена, выданного взамен при ротации"
    )


class Role(Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="Системные роли нельзя удалить",
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "role_code", name="user_roles_pk"),
        CheckConstraint("source IN ('local', 'ldap')", name="user_roles_source_chk"),
        {
            "comment": (
                "Роли из каталога — кэш с TTL, перезаписывается при входе. Роли "
                "source=local назначаются администратором и синхронизацией не затрагиваются."
            )
        },
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(
        Text, ForeignKey("roles.code", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'local'")
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RoleMapping(Base):
    """Маппинг групп каталога во внутренние роли, используется при синхронизации."""

    __tablename__ = "role_mappings"
    __table_args__ = (
        UniqueConstraint("external_group", "role_code", name="role_mappings_uq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    external_group: Mapped[str] = mapped_column(
        Text, nullable=False, comment="DN группы в AD"
    )
    role_code: Mapped[str] = mapped_column(
        Text, ForeignKey("roles.code", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
