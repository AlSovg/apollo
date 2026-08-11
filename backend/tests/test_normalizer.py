"""Нормализатор против реальной PostgreSQL.

Проверяется поведение конвейера целиком: объект, инцидент, лента, дедупликация,
устойчивость к битым событиям и к перезапуску в середине пачки.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.events import Incident, IncidentEvent, MonitoredObject, RawEvent
from app.services.normalizer import fingerprint, is_suppressed, process_batch
from tests.conftest import load_fixture


async def add_raw_event(
    session: AsyncSession, source: str, payload: dict[str, Any]
) -> RawEvent:
    """Событие в очереди нормализации.

    Хэш случайный: дедуп повторной доставки — это забота приёма, а здесь
    нужны именно повторы одного и того же события (внутри одной транзакции
    now() постоянна, и одинаковые хэши упёрлись бы в raw_events_dedup_uq).
    """
    raw = RawEvent(source=source, payload=payload, payload_hash=secrets.token_bytes(32))
    session.add(raw)
    await session.flush()
    return raw


async def counts(session: AsyncSession) -> tuple[int, int, int]:
    incidents = await session.scalar(select(func.count()).select_from(Incident))
    events = await session.scalar(select(func.count()).select_from(IncidentEvent))
    objects = await session.scalar(select(func.count()).select_from(MonitoredObject))
    return int(incidents or 0), int(events or 0), int(objects or 0)


async def test_problem_and_resolved_make_one_incident(db_session: AsyncSession) -> None:
    """Два вебхука Zabbix об одном event_id — это один инцидент."""
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_resolved.json"))

    result = await process_batch(db_session)

    assert result.processed == 2
    assert result.failed == 0

    incidents, events, objects = await counts(db_session)
    assert (incidents, events, objects) == (1, 2, 1)

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.external_key == "59"
    assert incident.status == "resolved"
    assert incident.resolved_at is not None
    assert incident.duration_s == 28  # 10:53:52 → 10:54:20
    assert incident.severity == 4
    assert incident.rule_name == "Test webhook trigger"


async def test_all_fixtures_give_two_incidents_and_three_events(
    db_session: AsyncSession,
) -> None:
    """Приёмка шага: пример в конце db/schema.sql."""
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_resolved.json"))
    await add_raw_event(db_session, "solarwinds", load_fixture("solarwinds_trigger.json"))

    result = await process_batch(db_session)

    assert result.failed == 0
    incidents, events, objects = await counts(db_session)
    assert incidents == 2
    assert events == 3
    assert objects == 2


async def test_resolved_arriving_before_problem_keeps_correct_state(
    db_session: AsyncSession,
) -> None:
    """Порядок доставки вебхуков не гарантирован (правило 3 AGENTS.md)."""
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_resolved.json"))
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))

    await process_batch(db_session)

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "resolved"  # запоздавшая проблема не переоткрывает
    assert incident.resolved_at is not None
    # Начало уточнилось по опоздавшему PROBLEM — duration_s остался верным
    assert incident.duration_s == 28

    events = await db_session.scalar(select(func.count()).select_from(IncidentEvent))
    assert events == 2  # в ленту попали оба события


async def test_repeated_problems_increment_dedup_count(db_session: AsyncSession) -> None:
    for _ in range(3):
        await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))

    await process_batch(db_session)

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.dedup_count == 3
    assert incident.status == "open"

    events = await db_session.scalar(select(func.count()).select_from(IncidentEvent))
    assert events == 3  # лента фиксирует каждое событие


async def test_problem_after_resolve_reopens_incident(db_session: AsyncSession) -> None:
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_resolved.json"))
    await process_batch(db_session)

    reopening = load_fixture("zabbix_problem.json")
    reopening["timestamp"] = "2026.08.10 11:10:00"
    await add_raw_event(db_session, "zabbix", reopening)
    await process_batch(db_session)

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "open"
    assert incident.resolved_at is None
    assert incident.opened_at.astimezone(UTC) == datetime(2026, 8, 10, 8, 10, tzinfo=UTC)


async def test_stale_event_does_not_roll_back_state(db_session: AsyncSession) -> None:
    """Событие старше уже учтённого попадает в ленту, но не меняет состояние."""
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_resolved.json"))
    await process_batch(db_session)

    stale = load_fixture("zabbix_problem.json")
    stale["timestamp"] = "2026.08.10 09:00:00"
    stale["severity"] = "Disaster"
    stale["event_nseverity"] = "5"
    await add_raw_event(db_session, "zabbix", stale)
    await process_batch(db_session)

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "resolved"  # состояние не откатилось
    assert incident.severity == 4  # severity тоже не перезаписан старым событием

    events = await db_session.scalar(select(func.count()).select_from(IncidentEvent))
    assert events == 3


async def test_broken_event_does_not_stop_the_batch(db_session: AsyncSession) -> None:
    broken = await add_raw_event(db_session, "zabbix", {"мусор": "без обязательных полей"})
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))

    result = await process_batch(db_session)

    assert result.processed == 1
    assert result.failed == 1

    await db_session.refresh(broken)
    assert broken.process_error is not None
    # processed_at НЕ выставлен: событие переобработается после исправления
    assert broken.processed_at is None

    incidents, events, _ = await counts(db_session)
    assert (incidents, events) == (1, 1)


async def test_unknown_severity_is_reported_as_process_error(
    db_session: AsyncSession,
) -> None:
    payload = load_fixture("zabbix_problem.json")
    payload["event_nseverity"] = "9"
    raw = await add_raw_event(db_session, "zabbix", payload)

    result = await process_batch(db_session)

    assert result.failed == 1
    await db_session.refresh(raw)
    assert raw.process_error is not None
    assert "severity_map" in raw.process_error


async def test_processed_events_are_not_picked_up_twice(db_session: AsyncSession) -> None:
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))

    first = await process_batch(db_session)
    second = await process_batch(db_session)

    assert first.size == 1
    assert second.size == 0  # очередь пуста

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.dedup_count == 1  # повторной обработки не было


async def test_crash_in_the_middle_of_a_batch_loses_nothing(
    db_session: AsyncSession,
) -> None:
    """Падение воркера = откат транзакции: ни потерь, ни двойной обработки."""
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_resolved.json"))
    await db_session.commit()

    # Пачка разобрана, но транзакция не закоммичена — имитация падения
    savepoint = await db_session.begin_nested()
    await process_batch(db_session)
    await savepoint.rollback()

    unprocessed = await db_session.scalar(
        select(func.count()).select_from(RawEvent).where(RawEvent.processed_at.is_(None))
    )
    assert unprocessed == 2  # события на месте
    incidents, _, _ = await counts(db_session)
    assert incidents == 0  # ничего не записалось

    result = await process_batch(db_session)

    assert result.processed == 2
    incidents, events, _ = await counts(db_session)
    assert (incidents, events) == (1, 2)  # обработаны ровно по одному разу


async def test_object_is_deduplicated_and_last_seen_updated(
    db_session: AsyncSession,
) -> None:
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))
    await add_raw_event(db_session, "zabbix", load_fixture("zabbix_resolved.json"))

    await process_batch(db_session, now=datetime(2026, 8, 10, 12, 0, tzinfo=UTC))

    obj = (await db_session.execute(select(MonitoredObject))).scalar_one()
    assert obj.source == "zabbix"
    assert obj.external_id == "webhook-test-host"
    assert obj.last_seen.astimezone(UTC) == datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


async def test_incident_fingerprint_is_stable_and_specific() -> None:
    same = fingerprint("zabbix", "db-prod-03", "25239")
    assert same == fingerprint("zabbix", "db-prod-03", "25239")
    assert same != fingerprint("zabbix", "db-prod-03", "25240")
    assert same != fingerprint("solarwinds", "db-prod-03", "25239")
    # Разделитель обязателен: иначе ('a','bc') и ('ab','c') дали бы один отпечаток
    assert fingerprint("zabbix", "a", "bc") != fingerprint("zabbix", "ab", "c")


@pytest.mark.parametrize(
    ("kind", "minutes_ago", "expected"),
    [
        ("problem", 5, True),  # повтор внутри окна подавления
        ("problem", 20, False),  # окно истекло — уведомляем снова
        ("resolved", 5, False),  # закрытие подавлять нельзя
    ],
)
def test_suppression_window(kind: str, minutes_ago: int, expected: bool) -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    assert (
        is_suppressed(kind, now - timedelta(minutes=minutes_ago), now, timedelta(minutes=15))
        is expected
    )


def test_never_notified_incident_is_not_suppressed() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    assert is_suppressed("problem", None, now, timedelta(minutes=15)) is False


async def test_source_registry_remembers_last_event(db_session: AsyncSession) -> None:
    """Панель «состояние интеграций» должна видеть молчащий источник."""
    from app.models.events import EventSource

    raw = await add_raw_event(db_session, "zabbix", load_fixture("zabbix_problem.json"))
    await process_batch(db_session)

    source = await db_session.get(EventSource, "zabbix")
    assert source is not None
    await db_session.refresh(source)
    assert source.last_event_at == raw.received_at
