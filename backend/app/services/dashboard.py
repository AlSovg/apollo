"""Агрегаты административного дашборда платформы.

Показывает состояние ПЛАТФОРМЫ, а не инцидентов. Всё считается запросами по
raw_events, incidents и notifications: отдельной системы метрик в контуре
не появляется — это осознанное решение из AGENTS.md, меньше компонентов,
меньше поверхность атаки.

Главная цифра защиты — коэффициент дедупликации: во сколько раз поток событий
сжался в инциденты.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PERIODS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def period_start(period: str, now: datetime | None = None) -> datetime:
    delta = PERIODS.get(period)
    if delta is None:
        raise ValueError(f"Неизвестный период: {period}")
    return (now or datetime.now(tz=UTC)) - delta


# --- нагрузка ---------------------------------------------------------------


@dataclass
class LoadPoint:
    bucket: datetime
    source: str
    events: int


@dataclass
class LoadStats:
    events_per_minute: list[LoadPoint] = field(default_factory=list)
    normalization_backlog: int = 0
    delivery_backlog: int = 0
    events_total: int = 0


_LOAD_SERIES = text(
    """
    SELECT date_trunc('minute', received_at) AS bucket, source, count(*) AS events
    FROM raw_events
    WHERE received_at >= :since
    GROUP BY 1, 2
    ORDER BY 1
    """
)

_BACKLOG = text(
    """
    SELECT
      (SELECT count(*) FROM raw_events WHERE processed_at IS NULL AND process_error IS NULL)
        AS normalization_backlog,
      (SELECT count(*) FROM notifications WHERE status = 'pending') AS delivery_backlog
    """
)


async def load_stats(session: AsyncSession, since: datetime) -> LoadStats:
    series = await session.execute(_LOAD_SERIES, {"since": since})
    points = [
        LoadPoint(bucket=row.bucket, source=row.source, events=row.events) for row in series
    ]
    backlog = (await session.execute(_BACKLOG)).one()

    return LoadStats(
        events_per_minute=points,
        normalization_backlog=backlog.normalization_backlog,
        delivery_backlog=backlog.delivery_backlog,
        events_total=sum(point.events for point in points),
    )


# --- обработка и дедупликация ----------------------------------------------


@dataclass
class ProcessingStats:
    events_received: int = 0
    events_normalized: int = 0
    normalization_errors: int = 0
    incidents_created: int = 0
    suppressed_repeats: int = 0
    dedup_ratio: float = 0.0
    noise_reduction: float = 1.0


_PROCESSING = text(
    """
    SELECT
      (SELECT count(*) FROM raw_events WHERE received_at >= :since) AS events_received,
      (SELECT count(*) FROM raw_events
        WHERE received_at >= :since AND processed_at IS NOT NULL) AS events_normalized,
      (SELECT count(*) FROM raw_events
        WHERE received_at >= :since AND process_error IS NOT NULL) AS normalization_errors,
      (SELECT count(*) FROM incidents WHERE created_at >= :since) AS incidents_created,
      (SELECT coalesce(sum(dedup_count) - count(*), 0) FROM incidents
        WHERE created_at >= :since) AS suppressed_repeats
    """
)


async def processing_stats(session: AsyncSession, since: datetime) -> ProcessingStats:
    row = (await session.execute(_PROCESSING, {"since": since})).one()

    received = row.events_received
    incidents = row.incidents_created
    # Коэффициент дедупликации = 1 − инцидентов/событий: доля событий,
    # которые не потребовали отдельного разбирательства
    ratio = 1 - incidents / received if received else 0.0
    return ProcessingStats(
        events_received=received,
        events_normalized=row.events_normalized,
        normalization_errors=row.normalization_errors,
        incidents_created=incidents,
        suppressed_repeats=int(row.suppressed_repeats or 0),
        dedup_ratio=round(ratio, 4),
        # Во сколько раз поток сжался: именно эту цифру называют вслух
        noise_reduction=round(received / incidents, 2) if incidents else 1.0,
    )


# --- доставка ---------------------------------------------------------------


@dataclass
class ChannelStats:
    channel: str
    sent: int = 0
    failed: int = 0
    pending: int = 0
    skipped: int = 0


@dataclass
class FailureReason:
    reason: str
    count: int


@dataclass
class DeliveryStats:
    sent: int = 0
    failed: int = 0
    pending: int = 0
    skipped: int = 0
    delivered_percent: float = 0.0
    by_channel: list[ChannelStats] = field(default_factory=list)
    failures: list[FailureReason] = field(default_factory=list)


_DELIVERY_BY_CHANNEL = text(
    """
    SELECT channel,
           count(*) FILTER (WHERE status = 'sent')    AS sent,
           count(*) FILTER (WHERE status = 'failed')  AS failed,
           count(*) FILTER (WHERE status = 'pending') AS pending,
           count(*) FILTER (WHERE status = 'skipped') AS skipped
    FROM notifications
    WHERE created_at >= :since
    GROUP BY channel
    ORDER BY channel
    """
)

_DELIVERY_FAILURES = text(
    """
    -- Колонку нельзя назвать count: у объекта строки уже есть такой метод
    SELECT coalesce(last_error, 'без описания') AS reason, count(*) AS total
    FROM notifications
    WHERE created_at >= :since AND status = 'failed'
    GROUP BY 1
    ORDER BY total DESC
    LIMIT 10
    """
)


async def delivery_stats(session: AsyncSession, since: datetime) -> DeliveryStats:
    rows = (await session.execute(_DELIVERY_BY_CHANNEL, {"since": since})).all()
    channels = [
        ChannelStats(
            channel=row.channel,
            sent=row.sent,
            failed=row.failed,
            pending=row.pending,
            skipped=row.skipped,
        )
        for row in rows
    ]
    failures = [
        FailureReason(reason=row.reason, count=row.total)
        for row in (await session.execute(_DELIVERY_FAILURES, {"since": since})).all()
    ]

    sent = sum(item.sent for item in channels)
    failed = sum(item.failed for item in channels)
    pending = sum(item.pending for item in channels)
    skipped = sum(item.skipped for item in channels)

    # Заглушённые подписки в знаменатель не идут: это осознанное решение
    # не доставлять, а не провал доставки
    attempted = sent + failed + pending
    return DeliveryStats(
        sent=sent,
        failed=failed,
        pending=pending,
        skipped=skipped,
        delivered_percent=round(100 * sent / attempted, 2) if attempted else 0.0,
        by_channel=channels,
        failures=failures,
    )


# --- топы -------------------------------------------------------------------


@dataclass
class TopEntry:
    name: str
    incidents: int
    events: int = 0


@dataclass
class TopStats:
    rules: list[TopEntry] = field(default_factory=list)
    objects: list[TopEntry] = field(default_factory=list)


_TOP_RULES = text(
    """
    SELECT rule_name AS name, count(*) AS incidents, coalesce(sum(dedup_count), 0) AS events
    FROM incidents
    WHERE created_at >= :since
    GROUP BY rule_name
    ORDER BY incidents DESC, events DESC
    LIMIT :limit
    """
)

_TOP_OBJECTS = text(
    """
    SELECT coalesce(o.name, '(объект не определён)') AS name,
           count(*) AS incidents,
           coalesce(sum(i.dedup_count), 0) AS events
    FROM incidents i
    LEFT JOIN monitored_objects o ON o.id = i.object_id
    WHERE i.created_at >= :since
    GROUP BY 1
    ORDER BY incidents DESC, events DESC
    LIMIT :limit
    """
)


async def top_stats(session: AsyncSession, since: datetime, limit: int = 10) -> TopStats:
    params = {"since": since, "limit": limit}
    rules = (await session.execute(_TOP_RULES, params)).all()
    objects = (await session.execute(_TOP_OBJECTS, params)).all()

    return TopStats(
        rules=[
            TopEntry(name=row.name, incidents=row.incidents, events=int(row.events))
            for row in rules
        ],
        objects=[
            TopEntry(name=row.name, incidents=row.incidents, events=int(row.events))
            for row in objects
        ],
    )


# --- задержки конвейера -----------------------------------------------------


@dataclass
class LatencySegment:
    segment: str
    samples: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    # Норматив участка из FR-ADM-6; сравнение ведётся по p95
    target_ms: int | None = None


@dataclass
class LatencyStats:
    segments: list[LatencySegment] = field(default_factory=list)


def _percentiles(expression: str, source: str, condition: str) -> str:
    return f"""
    SELECT count(*) AS samples,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY {expression}) AS p50,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY {expression}) AS p95,
           percentile_cont(0.99) WITHIN GROUP (ORDER BY {expression}) AS p99
    FROM {source}
    WHERE {condition}
    """


# Нормативы FR-ADM-6: вебхук → инцидент ≤ 2 с, событие → инбокс ≤ 5 с.
# Первый участок — это и есть «вебхук → инцидент», последний, сквозной, —
# «событие → инбокс»: без него панель показывала куски пути, а норматив
# в подписи относился к целому, и сравнивать их было не с чем.
# Сквозная задержка меряется от приёма события, породившего уведомление:
# прямой ссылки notification → событие нет, поэтому берётся последнее
# событие инцидента, попавшее в систему до создания уведомления.
_TRIGGERING_EVENT = """
    (SELECT max(e.received_at) FROM incident_events e
      WHERE e.incident_id = n.incident_id AND e.received_at <= n.created_at)
"""

_LATENCY_QUERIES: dict[str, tuple[str, int | None]] = {
    "приём → нормализация": (
        _percentiles(
            "extract(epoch FROM (processed_at - received_at))",
            "raw_events",
            "received_at >= :since AND processed_at IS NOT NULL",
        ),
        2000,
    ),
    "инцидент → уведомление": (
        _percentiles(
            "extract(epoch FROM (n.created_at - i.created_at))",
            "notifications n JOIN incidents i ON i.id = n.incident_id",
            "n.created_at >= :since",
        ),
        None,
    ),
    "уведомление → доставлено": (
        _percentiles(
            "extract(epoch FROM (sent_at - created_at))",
            "notifications",
            "created_at >= :since AND sent_at IS NOT NULL",
        ),
        None,
    ),
    "событие → инбокс (сквозная)": (
        _percentiles(
            f"extract(epoch FROM (n.sent_at - {_TRIGGERING_EVENT}))",
            "notifications n",
            f"n.created_at >= :since AND n.sent_at IS NOT NULL "
            f"AND {_TRIGGERING_EVENT} IS NOT NULL",
        ),
        5000,
    ),
}


async def latency_stats(session: AsyncSession, since: datetime) -> LatencyStats:
    segments = []
    for name, (query, target_ms) in _LATENCY_QUERIES.items():
        row = (await session.execute(text(query), {"since": since})).one()
        segments.append(
            LatencySegment(
                segment=name,
                samples=row.samples,
                p50_ms=round(row.p50 * 1000, 1) if row.p50 is not None else None,
                p95_ms=round(row.p95 * 1000, 1) if row.p95 is not None else None,
                p99_ms=round(row.p99 * 1000, 1) if row.p99 is not None else None,
                target_ms=target_ms,
            )
        )
    return LatencyStats(segments=segments)


# --- состояние интеграций ---------------------------------------------------


@dataclass
class SourceHealth:
    code: str
    name: str
    is_active: bool
    last_event_at: datetime | None
    silent_minutes: float | None


_SOURCE_HEALTH = text(
    """
    SELECT s.code,
           s.name,
           s.is_active,
           (SELECT max(received_at) FROM raw_events r WHERE r.source = s.code)
             AS last_event_at
    FROM event_sources s
    ORDER BY s.code
    """
)


async def source_health(
    session: AsyncSession, now: datetime | None = None
) -> list[SourceHealth]:
    """Молчащий источник виден сразу — это опора панели «состояние интеграций»."""
    moment = now or datetime.now(tz=UTC)
    rows = (await session.execute(_SOURCE_HEALTH)).all()

    return [
        SourceHealth(
            code=row.code,
            name=row.name,
            is_active=row.is_active,
            last_event_at=row.last_event_at,
            silent_minutes=(
                round((moment - row.last_event_at).total_seconds() / 60, 1)
                if row.last_event_at
                else None
            ),
        )
        for row in rows
    ]
