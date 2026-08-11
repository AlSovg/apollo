"""Веб-инбокс дежурного."""

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.channels import WebChannel
from app.adapters.contacts import LocalContactResolver
from app.models.delivery import LlmSummary, Notification
from app.services.delivery import deliver_batch
from app.services.router import route_batch
from tests.factories import PASSWORD, make_incident, make_topic, make_user, subscribe


async def _deliver(db_session: AsyncSession) -> None:
    await route_batch(db_session)
    await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
    )


async def login(client: httpx.AsyncClient, username: str) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200


async def test_inbox_shows_delivered_notification(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    incident = await make_incident(db_session)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    body = (await db_client.get("/api/v1/notifications")).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["incident_id"] == incident.id
    assert item["rule_name"] == "Диск заполнен на 94%"
    assert item["object_name"] == "db-prod-03"
    assert item["severity"] == 4
    assert item["read_at"] is None


async def test_pending_notification_is_not_in_inbox(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Инбокс показывает доставленное, а не поставленное в очередь."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session)
    await route_batch(db_session)
    await db_session.commit()

    await login(db_client, "duty")

    assert (await db_client.get("/api/v1/notifications")).json()["total"] == 0


async def test_inbox_is_private(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "duty")
    other = await make_user(db_session, "someone")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, owner, topic)
    await make_incident(db_session)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "someone")
    assert (await db_client.get("/api/v1/notifications")).json()["total"] == 0
    assert other.id != owner.id


async def test_unread_count_and_mark_read(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session)
    await make_incident(db_session)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    assert (await db_client.get("/api/v1/notifications/unread-count")).json()["unread"] == 2

    first = (await db_client.get("/api/v1/notifications")).json()["items"][0]
    marked = await db_client.post(
        f"/api/v1/notifications/{first['notification_id']}/read"
    )
    assert marked.status_code == 200
    assert marked.json()["updated"] == 1
    assert (await db_client.get("/api/v1/notifications/unread-count")).json()["unread"] == 1

    # Повторная отметка ничего не меняет
    repeated = await db_client.post(f"/api/v1/notifications/{first['notification_id']}/read")
    assert repeated.json()["updated"] == 0


async def test_read_all(db_client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    for _ in range(3):
        await make_incident(db_session)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    result = await db_client.post("/api/v1/notifications/read-all")

    assert result.json()["updated"] == 3
    assert (await db_client.get("/api/v1/notifications/unread-count")).json()["unread"] == 0


async def test_unread_only_filter(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session)
    await make_incident(db_session)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    first = (await db_client.get("/api/v1/notifications")).json()["items"][0]
    await db_client.post(f"/api/v1/notifications/{first['notification_id']}/read")

    unread = (await db_client.get("/api/v1/notifications?unread_only=true")).json()
    assert unread["total"] == 1


async def test_inbox_exact_severity_matches_its_counter(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Число на плитке инбокса должно совпадать с числом карточек."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session, severity=5)
    await make_incident(db_session, severity=3)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    body = (await db_client.get("/api/v1/notifications?severity=3")).json()

    assert body["total"] == body["severity_counts"]["3"] == 1
    assert {item["severity"] for item in body["items"]} == {3}


async def test_inbox_severity_filter(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """В инбоксе за смену копятся сотни писем: без отбора по критичности
    дежурный не найдёт аварию среди предупреждений."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session, severity=5)
    await make_incident(db_session, severity=2)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    body = (await db_client.get("/api/v1/notifications?severity_min=5")).json()

    assert body["total"] == 1
    assert body["items"][0]["severity"] == 5
    # Плитки нижних уровней остаются видимыми — иначе фильтр слепнет
    assert body["severity_counts"]["5"] == 1
    assert body["severity_counts"]["2"] == 1


async def test_inbox_search_and_status_filters(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")

    found = await db_client.get("/api/v1/notifications?search=db-prod")
    assert found.json()["total"] == 1
    missing = await db_client.get("/api/v1/notifications?search=такого нет")
    assert missing.json()["total"] == 0
    resolved = await db_client.get("/api/v1/notifications?status=resolved")
    assert resolved.json()["total"] == 0


async def test_inbox_counts_respect_other_filters(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Поиск сужает и счётчики: плитка не должна обещать того, чего в выборке нет."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session, severity=5)
    await make_incident(db_session, severity=2)
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    body = (await db_client.get("/api/v1/notifications?search=такого нет")).json()

    assert body["severity_counts"] == {str(level): 0 for level in range(6)}


async def test_summary_is_shown_when_present(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Сводка модели попадает в инбокс через вьюху."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    incident = await make_incident(db_session)
    db_session.add(
        LlmSummary(
            incident_id=incident.id,
            model="local-model",
            prompt_version="v1",
            content="Диск на db-prod-03 почти заполнен, проверьте ротацию логов.",
        )
    )
    await _deliver(db_session)
    await db_session.commit()

    await login(db_client, "duty")
    item = (await db_client.get("/api/v1/notifications")).json()["items"][0]

    assert item["summary"] is not None
    assert "ротацию логов" in item["summary"]


async def test_delivery_history_has_no_address(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """История доставки показывает канал, статус, попытки и ошибку — но не адрес."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic, channels=["web", "email"])
    await make_incident(db_session)
    await _deliver(db_session)
    await db_session.execute(
        update(Notification)
        .where(Notification.channel == "email")
        .values(status="failed", attempts=3, last_error="SMTP недоступен: TimeoutError")
    )
    await db_session.commit()

    await login(db_client, "duty")
    web = (await db_client.get("/api/v1/notifications")).json()["items"][0]
    history = (
        await db_client.get(
            f"/api/v1/notifications/{web['notification_id']}/delivery"
        )
    ).json()

    assert {row["channel"] for row in history} == {"web", "email"}
    email = next(row for row in history if row["channel"] == "email")
    assert email["status"] == "failed"
    assert email["attempts"] == 3
    assert "TimeoutError" in email["last_error"]
    assert "address" not in email


async def test_foreign_notification_is_not_readable(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "duty")
    await make_user(db_session, "someone")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, owner, topic)
    await make_incident(db_session)
    await _deliver(db_session)
    await db_session.commit()

    notification_id = (
        await db_session.execute(select(Notification.id).limit(1))
    ).scalar_one()

    await login(db_client, "someone")
    response = await db_client.post(f"/api/v1/notifications/{notification_id}/read")

    assert response.status_code == 404


async def test_inbox_requires_authentication(db_client: httpx.AsyncClient) -> None:
    assert (await db_client.get("/api/v1/notifications")).status_code == 401
