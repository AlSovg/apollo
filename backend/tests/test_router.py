"""Маршрутизация инцидентов в уведомления."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import Notification
from app.models.events import Incident
from app.services.router import route_batch
from tests.factories import make_incident, make_topic, make_user, subscribe


async def notifications(session: AsyncSession) -> list[Notification]:
    return list(
        (await session.execute(select(Notification).order_by(Notification.id)))
        .scalars()
        .all()
    )


async def test_two_subscribers_get_two_notifications(db_session: AsyncSession) -> None:
    topic = await make_topic(db_session, match_rule={"sources": ["zabbix"]})
    first = await make_user(db_session, "duty1")
    second = await make_user(db_session, "duty2")
    await subscribe(db_session, first, topic)
    await subscribe(db_session, second, topic)
    incident = await make_incident(db_session)

    result = await route_batch(db_session)

    assert result.created == 2
    rows = await notifications(db_session)
    assert {row.user_id for row in rows} == {first.id, second.id}
    assert all(row.status == "pending" for row in rows)
    assert all(row.incident_id == incident.id for row in rows)


async def test_two_matching_topics_give_one_notification_per_channel(
    db_session: AsyncSession,
) -> None:
    """FR-DLV-2: ограничение notifications_uq делает это инвариантом схемы."""
    user = await make_user(db_session, "duty")
    first = await make_topic(db_session, "all", match_rule={})
    second = await make_topic(db_session, "prod-db", match_rule={"severity_min": 3})
    await subscribe(db_session, user, first)
    await subscribe(db_session, user, second)
    await make_incident(db_session)

    result = await route_batch(db_session)

    assert result.created == 1
    rows = await notifications(db_session)
    assert len(rows) == 1
    assert rows[0].topic_id in (first.id, second.id)


async def test_channels_of_subscription_are_respected(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session)
    await subscribe(db_session, user, topic, channels=["web", "email"])
    await make_incident(db_session)

    await route_batch(db_session)

    rows = await notifications(db_session)
    assert {row.channel for row in rows} == {"web", "email"}


async def test_muted_subscription_is_skipped_not_pending(db_session: AsyncSession) -> None:
    """Решение должно быть видно, а не пропасть молча."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session)
    await subscribe(
        db_session, user, topic, muted_until=datetime.now(tz=UTC) + timedelta(hours=1)
    )
    await make_incident(db_session)

    result = await route_batch(db_session)

    assert result.created == 0
    assert result.skipped == 1
    rows = await notifications(db_session)
    assert [row.status for row in rows] == ["skipped"]


async def test_expired_mute_does_not_skip(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session)
    await subscribe(
        db_session, user, topic, muted_until=datetime.now(tz=UTC) - timedelta(minutes=1)
    )
    await make_incident(db_session)

    result = await route_batch(db_session)

    assert result.created == 1


async def test_mandatory_subscription_ignores_mute(db_session: AsyncSession) -> None:
    """Обязательная подписка бьёт глушение (раздел 4 AGENTS.md)."""
    user = await make_user(db_session, "duty", role="engineer")
    topic = await make_topic(db_session, roles=["engineer"])
    await subscribe(
        db_session, user, topic, muted_until=datetime.now(tz=UTC) + timedelta(hours=1)
    )
    await make_incident(db_session)

    result = await route_batch(db_session)

    assert result.created == 1
    assert result.skipped == 0


async def test_deactivated_user_gets_no_notifications(db_session: AsyncSession) -> None:
    """Учётка отключена → уведомления не создаются (FR-IDM-4)."""
    user = await make_user(db_session, "fired", is_active=False)
    topic = await make_topic(db_session)
    await subscribe(db_session, user, topic)
    await make_incident(db_session)

    result = await route_batch(db_session)

    assert result.created == 0
    assert await notifications(db_session) == []


async def test_topic_rule_filters_incidents(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    # Тема другого источника не должна поймать это событие
    await make_topic(db_session, "net", match_rule={"sources": ["solarwinds"]})
    subscribed = await make_topic(db_session, "db", match_rule={"sources": ["zabbix"]})
    await subscribe(db_session, user, subscribed)
    await make_incident(db_session, source="zabbix")

    result = await route_batch(db_session)

    assert result.created == 1


async def test_incident_not_matching_any_topic_creates_nothing(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={"severity_min": 5})
    await subscribe(db_session, user, topic)
    incident = await make_incident(db_session, severity=2)

    result = await route_batch(db_session)

    assert result.created == 0
    # Инцидент всё равно помечен обработанным: иначе маршрутизатор зациклится
    await db_session.refresh(incident)
    assert incident.last_notified_at is not None


async def test_test_incidents_do_not_notify(db_session: AsyncSession) -> None:
    """FR-INC-7: тестовые инциденты не порождают уведомлений."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session, is_test=True)

    result = await route_batch(db_session)

    assert result.size == 0
    assert await notifications(db_session) == []


async def test_incident_is_routed_once(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session)
    await subscribe(db_session, user, topic)
    await make_incident(db_session)

    first = await route_batch(db_session)
    second = await route_batch(db_session)

    assert first.created == 1
    assert second.size == 0
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_mandatory_role_subscription_without_rows(db_session: AsyncSession) -> None:
    """Подписки нет строкой, но уведомление создаётся."""
    user = await make_user(db_session, "duty", role="engineer")
    await make_topic(db_session, roles=["engineer"])
    await make_incident(db_session)

    result = await route_batch(db_session)

    assert result.created == 1
    rows = await notifications(db_session)
    assert rows[0].user_id == user.id
    assert rows[0].channel == "web"


async def test_object_group_rule_uses_monitored_object(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={"object_groups": ["prod-db"]})
    await subscribe(db_session, user, topic)
    await make_incident(db_session, object_group="prod-db")
    await make_incident(db_session, object_group="test-lab")

    result = await route_batch(db_session)

    assert result.size == 2
    assert result.created == 1  # второй инцидент под правило не подошёл


async def test_last_notified_at_is_set(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session)
    await subscribe(db_session, user, topic)
    incident = await make_incident(db_session)
    moment = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    await route_batch(db_session, now=moment)

    stored = await db_session.get(Incident, incident.id)
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.last_notified_at == moment
