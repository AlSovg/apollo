"""Маршрутизация: инцидент → темы → эффективные подписчики → notifications.

Ограничение notifications_uq (incident_id, user_id, channel) делает правило
FR-DLV-2 инвариантом схемы, а не соглашением в коде: сколько бы тем ни подошло,
пользователь получит по инциденту одно уведомление на канал.

Тестовые инциденты уведомлений не порождают (FR-INC-7).
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Select, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import Notification
from app.models.events import Incident, MonitoredObject
from app.models.subscriptions import Topic
from app.services.topics import IncidentFacts, match_topic

logger = logging.getLogger(__name__)


@dataclass
class RoutedIncident:
    incident_id: int
    topics: list[int] = field(default_factory=list)
    created: int = 0
    skipped: int = 0


@dataclass
class RoutingResult:
    incidents: list[RoutedIncident] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.incidents)

    @property
    def created(self) -> int:
        return sum(item.created for item in self.incidents)

    @property
    def skipped(self) -> int:
        return sum(item.skipped for item in self.incidents)


def select_unrouted(limit: int) -> Select[tuple[Incident, str | None]]:
    """Инциденты, по которым уведомления ещё не создавались.

    Маркер — last_notified_at: пустой означает «не маршрутизировался».
    Группа объекта нужна правилам тем, поэтому подтягивается сразу.
    """
    return (
        select(Incident, MonitoredObject.object_group)
        .join(MonitoredObject, MonitoredObject.id == Incident.object_id, isouter=True)
        .where(Incident.last_notified_at.is_(None), Incident.is_test.is_(False))
        .order_by(Incident.opened_at)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Incident)
    )


async def load_active_topics(session: AsyncSession) -> list[Topic]:
    return list(
        (await session.execute(select(Topic).where(Topic.is_active).order_by(Topic.id)))
        .scalars()
        .all()
    )


def matching_topics(
    incident: Incident, object_group: str | None, topics: list[Topic]
) -> list[Topic]:
    facts = IncidentFacts(
        source=incident.source,
        severity=incident.severity,
        object_group=object_group,
        tags=list(incident.tags or []),
    )
    return [topic for topic in topics if match_topic(facts, topic.match_rule or {})]


_SUBSCRIBERS_QUERY = text(
    """
    SELECT e.user_id, e.topic_id, e.channels, e.muted_until, e.is_mandatory
    FROM v_effective_subscriptions e
    WHERE e.topic_id = ANY(:topic_ids)
    ORDER BY e.topic_id
    """
)


@dataclass
class Recipient:
    user_id: int
    topic_id: int
    channel: str
    muted: bool


async def collect_recipients(
    session: AsyncSession, topic_ids: list[int], *, now: datetime
) -> list[Recipient]:
    """Развернуть темы в пары (пользователь, канал).

    Вьюха уже отсеяла отключённых пользователей и неактивные темы, а у
    обязательных подписок обнулила muted_until — глушение их не отключает.
    Первая совпавшая тема выигрывает: остальные для того же канала отсекает
    ограничение notifications_uq.
    """
    rows = await session.execute(_SUBSCRIBERS_QUERY, {"topic_ids": topic_ids})

    seen: set[tuple[int, str]] = set()
    recipients: list[Recipient] = []
    for row in rows:
        muted = row.muted_until is not None and row.muted_until > now
        for channel in row.channels or []:
            key = (row.user_id, channel)
            if key in seen:
                continue
            seen.add(key)
            recipients.append(
                Recipient(
                    user_id=row.user_id, topic_id=row.topic_id, channel=channel, muted=muted
                )
            )
    return recipients


async def route_incident(
    session: AsyncSession, incident: Incident, object_group: str | None, topics: list[Topic],
    *, now: datetime,
) -> RoutedIncident:
    matched = matching_topics(incident, object_group, topics)
    result = RoutedIncident(incident_id=incident.id, topics=[topic.id for topic in matched])

    if matched:
        recipients = await collect_recipients(
            session, [topic.id for topic in matched], now=now
        )
        for recipient in recipients:
            # Заглушённая подписка даёт строку skipped: решение должно быть
            # видно в дашборде, а не пропасть молча
            status = "skipped" if recipient.muted else "pending"
            statement = (
                insert(Notification)
                .values(
                    incident_id=incident.id,
                    user_id=recipient.user_id,
                    topic_id=recipient.topic_id,
                    channel=recipient.channel,
                    status=status,
                )
                .on_conflict_do_nothing(constraint="notifications_uq")
                .returning(Notification.id)
            )
            created = (await session.execute(statement)).scalar_one_or_none()
            if created is None:
                continue
            if status == "skipped":
                result.skipped += 1
            else:
                result.created += 1

    # Инцидент помечается обработанным в любом случае: иначе маршрутизатор
    # будет возвращаться к нему бесконечно
    await session.execute(
        update(Incident).where(Incident.id == incident.id).values(last_notified_at=now)
    )
    return result


async def route_batch(
    session: AsyncSession, *, limit: int = 100, now: datetime | None = None
) -> RoutingResult:
    """Разобрать пачку неотмаршрутизированных инцидентов."""
    moment = now or datetime.now(tz=UTC)
    result = RoutingResult()

    rows = (await session.execute(select_unrouted(limit))).all()
    if not rows:
        return result

    topics = await load_active_topics(session)
    for incident, object_group in rows:
        routed = await route_incident(session, incident, object_group, topics, now=moment)
        result.incidents.append(routed)
        logger.info(
            "incident routed",
            extra={
                "incident_id": incident.id,
                "topics": len(routed.topics),
                "notifications": routed.created,
                "skipped": routed.skipped,
            },
        )
    return result
