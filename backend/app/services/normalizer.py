"""Нормализация: raw_events → monitored_objects, incidents, incident_events.

Разбор пачки идёт запросом с FOR UPDATE SKIP LOCKED, поэтому воркеров можно
запускать несколько без координатора. Каждая запись обрабатывается в своей
точке сохранения: одно битое событие не останавливает поток (FR-NORM-5).

Инциденты пишутся только через INSERT ... ON CONFLICT DO UPDATE по
(source, external_key) — порядок доставки вебхуков не гарантирован, и RESOLVED
вполне может прийти раньше PROBLEM (правило 3 из AGENTS.md).
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement, case

from app.adapters.sources import (
    AdapterError,
    CanonicalEvent,
    SeverityMap,
    adapter_for_source,
    resolve_timezone,
)
from app.adapters.sources.registry import UnknownAdapterError
from app.models.events import EventSource, Incident, IncidentEvent, MonitoredObject, RawEvent
from app.models.system import SeverityMap as SeverityMapRow
from app.services.partitions import ensure_partition_for

logger = logging.getLogger(__name__)


@dataclass
class RecordResult:
    raw_event_id: int
    incident_id: int | None = None
    kind: str | None = None
    # Повтор в окне подавления: счётчик растёт, уведомление не порождается
    suppressed: bool = False
    error: str | None = None


@dataclass
class BatchResult:
    records: list[RecordResult] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return sum(1 for record in self.records if record.error is None)

    @property
    def failed(self) -> int:
        return sum(1 for record in self.records if record.error is not None)

    @property
    def size(self) -> int:
        return len(self.records)


def fingerprint(source: str, object_external_id: str, rule_key: str) -> bytes:
    """Ключ дедупликации.

    Части разделены '|': без разделителя ('a' + 'bc') и ('ab' + 'c') дали бы
    один и тот же отпечаток для разных инцидентов.
    """
    return hashlib.sha256(f"{source}|{object_external_id}|{rule_key}".encode()).digest()


def select_unprocessed(limit: int) -> Select[tuple[RawEvent]]:
    """Очередь нормализации. Запрос зафиксирован в конце db/schema.sql."""
    return (
        select(RawEvent)
        .where(RawEvent.processed_at.is_(None))
        .order_by(RawEvent.received_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def load_severity_map(session: AsyncSession) -> SeverityMap:
    rows = (await session.execute(select(SeverityMapRow))).scalars().all()
    return SeverityMap({(row.source, row.raw_value): row.severity for row in rows})


async def load_sources(session: AsyncSession) -> dict[str, EventSource]:
    rows = (await session.execute(select(EventSource))).scalars().all()
    return {row.code: row for row in rows}


async def upsert_monitored_object(
    session: AsyncSession, event: CanonicalEvent, moment: datetime
) -> int:
    """Объект живёт дольше инцидентов: повторная встреча обновляет last_seen."""
    statement = (
        insert(MonitoredObject)
        .values(
            source=event.source_system,
            external_id=event.object.external_id,
            name=event.object.name,
            ip=event.object.ip,
            object_group=event.object.object_group,
            last_seen=moment,
        )
        .on_conflict_do_update(
            constraint="monitored_objects_uq",
            set_={
                "name": insert(MonitoredObject).excluded.name,
                "ip": func.coalesce(
                    insert(MonitoredObject).excluded.ip, MonitoredObject.ip
                ),
                "object_group": func.coalesce(
                    insert(MonitoredObject).excluded.object_group,
                    MonitoredObject.object_group,
                ),
                "last_seen": insert(MonitoredObject).excluded.last_seen,
            },
        )
        .returning(MonitoredObject.id)
    )
    return int((await session.execute(statement)).scalar_one())


def _incident_values(event: CanonicalEvent, object_id: int) -> dict[str, object]:
    resolved = event.kind == "resolved"
    return {
        "source": event.source_system,
        "external_key": event.external_key,
        "fingerprint": fingerprint(
            event.source_system, event.object.external_id, event.rule.key or event.rule.name
        ),
        "object_id": object_id,
        "rule_key": event.rule.key,
        "rule_name": event.rule.name,
        "rule_expr": event.rule.expression,
        "severity": event.severity,
        "severity_raw": event.severity_raw,
        "status": "resolved" if resolved else "open",
        "opened_at": event.occurred_at,
        "resolved_at": event.occurred_at if resolved else None,
        "last_event_at": event.occurred_at,
        "dedup_count": 1,
        "last_message": event.message,
        "tags": event.tags,
        "is_test": event.is_test,
        "attrs": event.attrs,
    }


async def upsert_incident(session: AsyncSession, event: CanonicalEvent, object_id: int) -> int:
    """Состояние инцидента одним запросом, устойчивым к порядку событий."""
    statement = insert(Incident).values(**_incident_values(event, object_id))
    excluded = statement.excluded

    # Событие старше уже учтённого: в ленту оно попадёт, но состояние
    # инцидента откатывать нельзя
    is_stale: ColumnElement[bool] = excluded.last_event_at < Incident.last_event_at
    resolves = event.kind == "resolved"

    fresh: dict[str, ColumnElement[object]] = {
        column: case((is_stale, getattr(Incident, column)), else_=getattr(excluded, column))
        for column in (
            "fingerprint",
            "object_id",
            "rule_key",
            "rule_name",
            "rule_expr",
            "severity",
            "severity_raw",
            "last_message",
            "tags",
            "is_test",
            "attrs",
        )
    }

    upsert = statement.on_conflict_do_update(
        constraint="incidents_uq",
        set_={
            **fresh,
            "status": case((is_stale, Incident.status), else_=excluded.status),
            "resolved_at": case(
                (is_stale, Incident.resolved_at),
                else_=excluded.resolved_at,
            ),
            # Переоткрытие: проблема по закрытому инциденту начинает новый отсчёт.
            # Запоздавшая проблема лишь уточняет начало — так duration_s остаётся верным.
            "opened_at": case(
                (is_stale, func.least(Incident.opened_at, excluded.opened_at)),
                (
                    (Incident.status == "resolved") & (excluded.status == "open"),
                    excluded.opened_at,
                ),
                else_=Incident.opened_at,
            ),
            "last_event_at": func.greatest(Incident.last_event_at, excluded.last_event_at),
            "dedup_count": Incident.dedup_count + (0 if resolves else 1),
            "updated_at": func.now(),
        },
    ).returning(Incident.id)

    return int((await session.execute(upsert)).scalar_one())


async def insert_incident_event(
    session: AsyncSession, *, incident_id: int, raw_event: RawEvent, event: CanonicalEvent
) -> None:
    """Лента инцидента: только INSERT, никогда UPDATE."""
    await session.execute(
        insert(IncidentEvent).values(
            incident_id=incident_id,
            raw_event_id=raw_event.id,
            kind=event.kind,
            event_ts=event.occurred_at,
            received_at=raw_event.received_at,
            severity=event.severity,
            message=event.message,
            value=event.value,
            attrs=event.attrs,
        )
    )


async def _existing_incident(
    session: AsyncSession, event: CanonicalEvent
) -> tuple[int | None, datetime | None]:
    row = (
        await session.execute(
            select(Incident.id, Incident.last_notified_at).where(
                Incident.source == event.source_system,
                Incident.external_key == event.external_key,
            )
        )
    ).first()
    return (None, None) if row is None else (row[0], row[1])


def is_suppressed(
    kind: str, last_notified_at: datetime | None, now: datetime, window: timedelta
) -> bool:
    """Повтор в окне подавления не порождает нового уведомления (FR-INC-4)."""
    if kind != "problem" or last_notified_at is None:
        return False
    return now - last_notified_at < window


async def process_record(
    session: AsyncSession,
    raw_event: RawEvent,
    *,
    sources: dict[str, EventSource],
    severity_map: SeverityMap,
    zabbix_tz: str,
    suppress_window: timedelta,
    now: datetime,
) -> RecordResult:
    """Одна запись: адаптер → объект → инцидент → лента."""
    source = sources.get(raw_event.source)
    if source is None:
        raise UnknownAdapterError(f"Источник '{raw_event.source}' отсутствует в реестре")

    adapter = adapter_for_source(
        source, severity_map, resolve_timezone(source.timezone, zabbix_tz)
    )
    event = adapter.parse(raw_event.payload)

    # Партиция ленты определяется временем события в источнике, а не нашим
    await ensure_partition_for(
        await session.connection(), "incident_events", event.occurred_at.date()
    )

    _, last_notified_at = await _existing_incident(session, event)
    object_id = await upsert_monitored_object(session, event, now)
    incident_id = await upsert_incident(session, event, object_id)
    await insert_incident_event(
        session, incident_id=incident_id, raw_event=raw_event, event=event
    )

    return RecordResult(
        raw_event_id=raw_event.id,
        incident_id=incident_id,
        kind=event.kind,
        suppressed=is_suppressed(event.kind, last_notified_at, now, suppress_window),
    )


async def _touch_sources(session: AsyncSession, raw_events: list[RawEvent]) -> None:
    """Отметить в реестре, когда от источника последний раз приходили события.

    Одним запросом на пачку, а не на событие: на приёме такой записи быть
    не может (правило 1 — ровно один INSERT), а панель «состояние интеграций»
    должна видеть молчащий источник и по самой таблице реестра.
    """
    latest: dict[str, datetime] = {}
    for raw_event in raw_events:
        current = latest.get(raw_event.source)
        if current is None or raw_event.received_at > current:
            latest[raw_event.source] = raw_event.received_at

    for source, received_at in latest.items():
        await session.execute(
            update(EventSource)
            .where(
                EventSource.code == source,
                (EventSource.last_event_at.is_(None))
                | (EventSource.last_event_at < received_at),
            )
            .values(last_event_at=received_at)
        )


async def _mark_processed(session: AsyncSession, raw_event: RawEvent, moment: datetime) -> None:
    await session.execute(
        update(RawEvent)
        .where(RawEvent.id == raw_event.id, RawEvent.received_at == raw_event.received_at)
        .values(processed_at=moment, process_error=None)
    )


async def _mark_failed(session: AsyncSession, raw_event: RawEvent, error: str) -> None:
    """processed_at НЕ ставится: событие переобработается после исправления."""
    await session.execute(
        update(RawEvent)
        .where(RawEvent.id == raw_event.id, RawEvent.received_at == raw_event.received_at)
        .values(process_error=error[:2000])
    )


async def process_batch(
    session: AsyncSession,
    *,
    limit: int = 100,
    zabbix_tz: str = "Europe/Moscow",
    suppress_window: timedelta = timedelta(minutes=15),
    now: datetime | None = None,
) -> BatchResult:
    """Разобрать пачку необработанных событий.

    Транзакцией управляет вызывающий: пока она не закоммичена, строки остаются
    заблокированными, поэтому падение воркера в середине пачки не приводит ни
    к потере события, ни к повторной обработке.
    """
    moment = now or datetime.now(tz=UTC)
    result = BatchResult()

    raw_events = (await session.execute(select_unprocessed(limit))).scalars().all()
    if not raw_events:
        return result

    severity_map = await load_severity_map(session)
    sources = await load_sources(session)

    for raw_event in raw_events:
        try:
            async with session.begin_nested():
                record = await process_record(
                    session,
                    raw_event,
                    sources=sources,
                    severity_map=severity_map,
                    zabbix_tz=zabbix_tz,
                    suppress_window=suppress_window,
                    now=moment,
                )
                await _mark_processed(session, raw_event, moment)
        except (AdapterError, UnknownAdapterError, ValueError) as exc:
            # Ожидаемая ошибка разбора: помечаем событие и идём дальше
            message = f"{type(exc).__name__}: {exc}"
            await _mark_failed(session, raw_event, message)
            logger.warning(
                "normalization failed",
                extra={"raw_event_id": raw_event.id, "reason": type(exc).__name__},
            )
            result.records.append(RecordResult(raw_event_id=raw_event.id, error=message))
        except Exception as exc:  # noqa: BLE001 — одно битое событие не роняет поток
            message = f"{type(exc).__name__}: {exc}"
            await _mark_failed(session, raw_event, message)
            logger.exception("normalization error", extra={"raw_event_id": raw_event.id})
            result.records.append(RecordResult(raw_event_id=raw_event.id, error=message))
        else:
            result.records.append(record)

    await _touch_sources(session, list(raw_events))
    return result
