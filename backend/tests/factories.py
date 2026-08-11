"""Сборка тестовых данных: пользователи, темы, инциденты."""

import secrets
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import hash_password
from app.models.events import Incident, MonitoredObject
from app.models.identity import User, UserRole
from app.models.subscriptions import Subscription, Topic, TopicRole

PASSWORD = "правильный-пароль-12"


@lru_cache
def password_hash() -> str:
    """Один хеш на весь прогон.

    argon2 намеренно медленный (~50–100 мс), а пользователей тесты создают
    сотнями. Проверку самого хеширования делает test_auth_api, здесь важен
    только факт «пароль подходит».
    """
    return hash_password(PASSWORD)


async def make_user(
    session: AsyncSession, username: str, role: str | None = None, *, is_active: bool = True
) -> User:
    user = User(
        subject_id=f"{username}-uuid",
        provider="local",
        username=username,
        password_hash=password_hash(),
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    if role:
        session.add(UserRole(user_id=user.id, role_code=role, source="local"))
        await session.flush()
    return user


async def make_topic(
    session: AsyncSession,
    code: str = "prod-db",
    *,
    match_rule: dict[str, object] | None = None,
    roles: list[str] | None = None,
) -> Topic:
    topic = Topic(code=code, name=f"Тема {code}", match_rule=match_rule or {})
    session.add(topic)
    await session.flush()
    for role in roles or []:
        session.add(TopicRole(topic_id=topic.id, role_code=role))
    await session.flush()
    return topic


async def subscribe(
    session: AsyncSession,
    user: User,
    topic: Topic,
    *,
    origin: str = "self",
    channels: list[str] | None = None,
    muted_until: datetime | None = None,
    granted_by: int | None = None,
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        topic_id=topic.id,
        origin=origin,
        channels=channels or ["web"],
        muted_until=muted_until,
        granted_by=granted_by if origin == "assigned" else None,
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def make_incident(
    session: AsyncSession,
    *,
    source: str = "zabbix",
    severity: int = 4,
    tags: list[str] | None = None,
    object_group: str | None = "prod-db",
    is_test: bool = False,
    external_key: str | None = None,
    ip: str | None = "10.0.3.14",
) -> Incident:
    obj = MonitoredObject(
        source=source,
        external_id=f"host-{secrets.token_hex(4)}",
        name="db-prod-03",
        ip=ip,
        object_group=object_group,
    )
    session.add(obj)
    await session.flush()

    now = datetime.now(tz=UTC)
    incident = Incident(
        source=source,
        external_key=external_key or secrets.token_hex(8),
        fingerprint=secrets.token_bytes(32),
        object_id=obj.id,
        rule_key="25239",
        rule_name="Диск заполнен на 94%",
        severity=severity,
        severity_raw="High",
        status="open",
        opened_at=now,
        last_event_at=now,
        last_message="Диск заполнен на 94%",
        tags=tags or ["disk", "prod"],
        is_test=is_test,
    )
    session.add(incident)
    await session.flush()
    return incident
