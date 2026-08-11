"""Эффективные подписки и операции над ними.

Обязательные подписки не хранятся строками — они вычисляются из topic_roles
и user_roles. Слияние трёх происхождений с приоритетом
mandatory_role > assigned > self делает вьюха v_effective_subscriptions:
переписывать её логику на Python нельзя, иначе появятся две правды.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.models.subscriptions import Subscription, Topic

logger = logging.getLogger(__name__)

CHANNELS = ("web", "email", "trueconf")

# Происхождения, от которых пользователь не может отписаться сам
MANDATORY_ORIGINS = ("mandatory_role", "assigned")


@dataclass
class EffectiveSubscription:
    """Строка вьюхи плюс сведения о теме — то, что видит пользователь."""

    topic_id: int
    topic_code: str
    topic_name: str
    topic_description: str | None
    origin: str
    channels: list[str]
    muted_until: datetime | None
    is_mandatory: bool
    # id строки в subscriptions; у обязательной подписки его нет — её не существует
    subscription_id: int | None


_EFFECTIVE_QUERY = text(
    """
    SELECT e.topic_id,
           t.code        AS topic_code,
           t.name        AS topic_name,
           t.description AS topic_description,
           e.origin,
           e.channels,
           e.muted_until,
           e.is_mandatory,
           s.id          AS subscription_id
    FROM v_effective_subscriptions e
    JOIN topics t ON t.id = e.topic_id
    LEFT JOIN subscriptions s ON s.topic_id = e.topic_id AND s.user_id = e.user_id
    WHERE e.user_id = :user_id
    ORDER BY e.is_mandatory DESC, t.name
    """
)


async def get_effective_subscriptions(
    session: AsyncSession, user_id: int
) -> list[EffectiveSubscription]:
    rows = await session.execute(_EFFECTIVE_QUERY, {"user_id": user_id})
    return [
        EffectiveSubscription(
            topic_id=row.topic_id,
            topic_code=row.topic_code,
            topic_name=row.topic_name,
            topic_description=row.topic_description,
            origin=row.origin,
            channels=list(row.channels or []),
            muted_until=row.muted_until,
            is_mandatory=row.is_mandatory,
            # Строка subscriptions для обязательной по роли подписки не нужна
            subscription_id=row.subscription_id if row.origin != "mandatory_role" else None,
        )
        for row in rows
    ]


async def get_subscribers(session: AsyncSession, topic_id: int) -> list[int]:
    """Кому доставлять по теме. Понадобится маршрутизатору на шаге 7."""
    rows = await session.execute(
        text("SELECT user_id FROM v_effective_subscriptions WHERE topic_id = :topic_id"),
        {"topic_id": topic_id},
    )
    return [row.user_id for row in rows]


def validate_channels(channels: list[str]) -> list[str]:
    unknown = sorted(set(channels) - set(CHANNELS))
    if unknown:
        raise ApiError(
            400, "UNKNOWN_CHANNEL", f"Неизвестные каналы доставки: {', '.join(unknown)}"
        )
    if not channels:
        raise ApiError(400, "NO_CHANNELS", "Нужен хотя бы один канал доставки")
    return channels


def cap_mute(muted_until: datetime | None, *, max_hours: int) -> datetime | None:
    """Глушение ограничено сверху: подписка не должна замолкать навсегда."""
    if muted_until is None:
        return None
    limit = datetime.now(tz=UTC) + timedelta(hours=max_hours)
    return min(muted_until, limit)


async def active_topic(session: AsyncSession, topic_id: int) -> Topic:
    topic = await session.get(Topic, topic_id)
    if topic is None or not topic.is_active:
        raise ApiError(404, "TOPIC_NOT_FOUND", "Тема не найдена")
    return topic


async def get_subscription(session: AsyncSession, subscription_id: int) -> Subscription:
    subscription = await session.get(Subscription, subscription_id)
    if subscription is None:
        raise ApiError(404, "SUBSCRIPTION_NOT_FOUND", "Подписка не найдена")
    return subscription


async def self_subscribe(
    session: AsyncSession, *, user_id: int, topic_id: int, channels: list[str]
) -> Subscription:
    await active_topic(session, topic_id)
    existing = (
        await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id, Subscription.topic_id == topic_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiError(409, "ALREADY_SUBSCRIBED", "Подписка на эту тему уже есть")

    subscription = Subscription(
        user_id=user_id,
        topic_id=topic_id,
        origin="self",
        channels=validate_channels(channels),
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def self_unsubscribe(
    session: AsyncSession, *, user_id: int, subscription_id: int
) -> Subscription:
    """Отписаться можно только от добровольной подписки."""
    subscription = await get_subscription(session, subscription_id)
    if subscription.user_id != user_id:
        raise ApiError(404, "SUBSCRIPTION_NOT_FOUND", "Подписка не найдена")
    if subscription.origin != "self":
        raise ApiError(
            403,
            "SUBSCRIPTION_MANDATORY",
            "От назначенной подписки нельзя отказаться самостоятельно",
        )

    await session.execute(delete(Subscription).where(Subscription.id == subscription.id))
    return subscription
