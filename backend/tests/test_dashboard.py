"""Дашборд платформы: каждая цифра проверяется отдельно.

Коэффициент дедупликации и процент доставки будут называть вслух на защите,
поэтому считаются они на заранее известном наборе данных.
"""

import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.channels import WebChannel
from app.adapters.contacts import LocalContactResolver
from app.models.delivery import Notification
from app.models.events import Incident, IncidentEvent, RawEvent
from app.services.dashboard import (
    delivery_stats,
    latency_stats,
    load_stats,
    period_start,
    processing_stats,
    source_health,
    top_stats,
)
from app.services.delivery import deliver_batch
from app.services.router import route_batch
from tests.factories import PASSWORD, make_incident, make_topic, make_user, subscribe

SINCE = datetime.now(tz=UTC) - timedelta(hours=1)


async def add_raw_events(
    session: AsyncSession,
    count: int,
    *,
    source: str = "zabbix",
    processed: bool = True,
    error: str | None = None,
) -> None:
    now = datetime.now(tz=UTC)
    for _ in range(count):
        session.add(
            RawEvent(
                source=source,
                payload={"событие": "тест"},
                payload_hash=secrets.token_bytes(32),
                processed_at=now if processed else None,
                process_error=error,
            )
        )
    await session.flush()


# --- обработка и дедупликация ----------------------------------------------


async def test_dedup_ratio_is_computed_from_events_and_incidents(
    db_session: AsyncSession,
) -> None:
    """10 событий свернулись в 2 инцидента — шум упал в пять раз."""
    await add_raw_events(db_session, 10)
    first = await make_incident(db_session)
    second = await make_incident(db_session)
    await db_session.execute(
        update(Incident).where(Incident.id == first.id).values(dedup_count=6)
    )
    await db_session.execute(
        update(Incident).where(Incident.id == second.id).values(dedup_count=4)
    )
    await db_session.flush()

    stats = await processing_stats(db_session, SINCE)

    assert stats.events_received == 10
    assert stats.incidents_created == 2
    assert stats.dedup_ratio == 0.8  # 1 − 2/10
    assert stats.noise_reduction == 5.0  # 10 событий на 2 инцидента
    assert stats.suppressed_repeats == 8  # (6−1) + (4−1)


async def test_processing_counts_errors_separately(db_session: AsyncSession) -> None:
    await add_raw_events(db_session, 3)
    await add_raw_events(db_session, 2, processed=False, error="AdapterError: нет поля")

    stats = await processing_stats(db_session, SINCE)

    assert stats.events_received == 5
    assert stats.events_normalized == 3
    assert stats.normalization_errors == 2


async def test_empty_period_does_not_divide_by_zero(db_session: AsyncSession) -> None:
    stats = await processing_stats(db_session, SINCE)

    assert stats.events_received == 0
    assert stats.dedup_ratio == 0.0
    assert stats.noise_reduction == 1.0


# --- нагрузка ---------------------------------------------------------------


async def test_load_shows_events_per_minute_by_source(db_session: AsyncSession) -> None:
    await add_raw_events(db_session, 4, source="zabbix")
    await add_raw_events(db_session, 2, source="solarwinds")

    stats = await load_stats(db_session, SINCE)

    assert stats.events_total == 6
    by_source = {point.source: point.events for point in stats.events_per_minute}
    assert by_source == {"zabbix": 4, "solarwinds": 2}


async def test_backlog_counts_unprocessed_and_pending(db_session: AsyncSession) -> None:
    await add_raw_events(db_session, 3, processed=False)
    await add_raw_events(db_session, 1, processed=False, error="битое событие")
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session)
    await route_batch(db_session)

    stats = await load_stats(db_session, SINCE)

    # События с ошибкой в очередь не входят: они ждут исправления маппинга
    assert stats.normalization_backlog == 3
    assert stats.delivery_backlog == 1


# --- доставка ---------------------------------------------------------------


async def test_delivery_percent_and_channels(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic, channels=["web", "email"])
    await make_incident(db_session)
    await route_batch(db_session)
    await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
    )

    stats = await delivery_stats(db_session, SINCE)

    assert stats.sent == 1  # web доставлен
    assert stats.failed == 1  # email без адреса — отказ
    assert stats.delivered_percent == 50.0
    channels = {item.channel: item for item in stats.by_channel}
    assert channels["web"].sent == 1
    assert channels["email"].failed == 1
    assert stats.failures and stats.failures[0].count == 1


async def test_skipped_notifications_do_not_spoil_delivery_percent(
    db_session: AsyncSession,
) -> None:
    """Заглушённая подписка — осознанное решение не доставлять, а не провал."""
    user = await make_user(db_session, "duty")
    muted = await make_user(db_session, "resting")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await subscribe(
        db_session, muted, topic, muted_until=datetime.now(tz=UTC) + timedelta(hours=1)
    )
    await make_incident(db_session)
    await route_batch(db_session)
    await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
    )

    stats = await delivery_stats(db_session, SINCE)

    assert stats.sent == 1
    assert stats.skipped == 1
    assert stats.delivered_percent == 100.0


# --- топы -------------------------------------------------------------------


async def test_top_rules_and_objects(db_session: AsyncSession) -> None:
    noisy = await make_incident(db_session)
    await db_session.execute(
        update(Incident).where(Incident.id == noisy.id).values(dedup_count=7)
    )
    await make_incident(db_session)
    await db_session.flush()

    stats = await top_stats(db_session, SINCE)

    assert stats.rules[0].name == "Диск заполнен на 94%"
    assert stats.rules[0].incidents == 2
    assert stats.rules[0].events == 8  # 7 повторов + 1
    assert stats.objects[0].name == "db-prod-03"


# --- задержки ---------------------------------------------------------------


async def test_latency_percentiles_are_computed(db_session: AsyncSession) -> None:
    now = datetime.now(tz=UTC)
    for seconds in (1, 2, 3):
        session_event = RawEvent(
            source="zabbix",
            payload={},
            payload_hash=secrets.token_bytes(32),
            received_at=now - timedelta(minutes=5),
            processed_at=now - timedelta(minutes=5) + timedelta(seconds=seconds),
        )
        db_session.add(session_event)
    await db_session.flush()

    stats = await latency_stats(db_session, SINCE)
    segment = next(s for s in stats.segments if s.segment == "приём → нормализация")

    assert segment.samples == 3
    assert segment.p50_ms == 2000.0
    assert segment.p95_ms is not None and segment.p95_ms >= 2000.0


async def test_latency_without_data_returns_none(db_session: AsyncSession) -> None:
    stats = await latency_stats(db_session, SINCE)

    assert all(segment.samples == 0 for segment in stats.segments)
    assert all(segment.p50_ms is None for segment in stats.segments)


async def test_latency_targets_are_attached_to_regulated_segments(
    db_session: AsyncSession,
) -> None:
    """Норматив приходит вместе с замером: сравнивать цифру не с чем, если
    требование живёт отдельной подписью под таблицей."""
    stats = await latency_stats(db_session, SINCE)
    targets = {segment.segment: segment.target_ms for segment in stats.segments}

    assert targets["приём → нормализация"] == 2000
    assert targets["событие → инбокс (сквозная)"] == 5000
    # Промежуточные участки нормативов не имеют — их сумма и есть сквозной
    assert targets["инцидент → уведомление"] is None


async def test_end_to_end_latency_is_measured_from_triggering_event(
    db_session: AsyncSession,
) -> None:
    """Сквозной участок меряется от приёма события, а не от создания инцидента.

    У дедуплицированного инцидента created_at — это первое появление, и отсчёт
    от него превращал задержку доставки в возраст инцидента.
    """
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    incident = await make_incident(db_session)
    # Инцидент открыт давно, а событие, породившее уведомление, только что
    old = datetime.now(tz=UTC) - timedelta(minutes=30)
    await db_session.execute(
        update(Incident).where(Incident.id == incident.id).values(created_at=old)
    )
    raw = RawEvent(
        source="zabbix",
        payload={"событие": "тест"},
        payload_hash=secrets.token_bytes(32),
        processed_at=datetime.now(tz=UTC),
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        IncidentEvent(
            incident_id=incident.id,
            raw_event_id=raw.id,
            kind="problem",
            event_ts=datetime.now(tz=UTC),
            # Пара секунд назад: внутри одной транзакции created_at уведомления
            # берёт now() времени её старта, и событие «в тот же миг» оказалось
            # бы позже уведомления
            received_at=datetime.now(tz=UTC) - timedelta(seconds=2),
            severity=incident.severity,
            message="Диск заполнен на 94%",
        )
    )
    await db_session.flush()
    await route_batch(db_session)
    await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
    )
    await db_session.flush()

    stats = await latency_stats(db_session, SINCE)
    segment = next(s for s in stats.segments if s.segment == "событие → инбокс (сквозная)")

    assert segment.samples == 1
    assert segment.p50_ms is not None
    # Секунды, а не полчаса возраста инцидента
    assert segment.p50_ms < 60_000


# --- состояние источников ---------------------------------------------------


async def test_silent_source_is_visible(db_session: AsyncSession) -> None:
    now = datetime.now(tz=UTC)
    db_session.add(
        RawEvent(
            source="zabbix",
            payload={},
            payload_hash=secrets.token_bytes(32),
            received_at=now - timedelta(minutes=42),
        )
    )
    await db_session.flush()

    health = {item.code: item for item in await source_health(db_session, now=now)}

    assert health["zabbix"].silent_minutes == 42.0
    # Источник, от которого не приходило ничего, тоже виден
    assert health["solarwinds"].last_event_at is None
    assert health["solarwinds"].silent_minutes is None


# --- API --------------------------------------------------------------------


async def test_all_six_sections_are_available_to_admin(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    await db_session.commit()
    await db_client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    )

    for section in ("load", "processing", "delivery", "top", "latency", "integrations"):
        response = await db_client.get(f"/api/v1/admin/dashboard/{section}")
        assert response.status_code == 200, section


async def test_engineer_cannot_see_platform_dashboard(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "duty", role="engineer")
    await db_session.commit()
    await db_client.post(
        "/api/v1/auth/login", json={"username": "duty", "password": PASSWORD}
    )

    assert (await db_client.get("/api/v1/admin/dashboard/processing")).status_code == 403


async def test_unknown_period_is_rejected(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    await db_session.commit()
    await db_client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    )

    response = await db_client.get("/api/v1/admin/dashboard/processing?period=навсегда")

    assert response.status_code == 422


@pytest.mark.parametrize("period", ["1h", "24h", "7d", "30d"])
def test_all_periods_are_supported(period: str) -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    assert period_start(period, now) < now


async def test_integrations_section_reports_statuses(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    await db_session.commit()
    await db_client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    )

    body = (await db_client.get("/api/v1/admin/dashboard/integrations")).json()

    names = {item["name"] for item in body["integrations"]}
    assert names == {"Каталог (LDAP/AD)", "Языковая модель", "SMTP", "TrueConf"}
    assert {item["code"] for item in body["sources"]} >= {"zabbix", "solarwinds", "generic"}
    # Реквизиты и адреса в ответ не попадают
    assert all(
        item["detail"] is None or "://" not in item["detail"]
        for item in body["integrations"]
    )


async def test_delivery_section_returns_expected_numbers(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_user(db_session, "admin", role="admin")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, admin, topic)
    await make_incident(db_session)
    await route_batch(db_session)
    await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
    )
    await db_session.commit()

    await db_client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    body = (await db_client.get("/api/v1/admin/dashboard/delivery")).json()

    assert body["sent"] == 1
    assert body["delivered_percent"] == 100.0
    assert body["by_channel"][0]["channel"] == "web"


async def test_notifications_of_other_periods_are_excluded(
    db_session: AsyncSession,
) -> None:
    """Старые данные не должны портить картину выбранного периода."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    await make_incident(db_session)
    await route_batch(db_session)

    old = datetime.now(tz=UTC) - timedelta(days=10)
    await db_session.execute(update(Notification).values(created_at=old))
    await db_session.flush()

    recent = await delivery_stats(db_session, SINCE)
    monthly = await delivery_stats(db_session, datetime.now(tz=UTC) - timedelta(days=30))

    assert recent.pending == 0
    assert monthly.pending == 1
