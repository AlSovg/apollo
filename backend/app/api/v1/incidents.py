"""Инциденты: список для дашборда и карточка с лентой и сводкой."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, Row, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, current_user
from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_session
from app.models.delivery import LlmSummary
from app.models.events import Incident, IncidentEvent, MonitoredObject
from app.schemas.incidents import (
    IncidentCard,
    IncidentEventItem,
    IncidentListItem,
    IncidentPage,
    IncidentSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


IncidentRow = Row[tuple[Incident, str, str | None, str | None]]


def _to_item(row: IncidentRow) -> IncidentListItem:
    incident, object_name, object_ip, object_group = row
    # Колонка inet приходит объектом IPv4Address — наружу отдаём строкой
    address = str(object_ip) if object_ip else None
    return IncidentListItem(
        id=incident.id,
        source=incident.source,
        external_key=incident.external_key,
        rule_name=incident.rule_name,
        severity=incident.severity,
        severity_raw=incident.severity_raw,
        status=incident.status,
        object_name=object_name,
        object_ip=address,
        object_group=object_group,
        opened_at=incident.opened_at,
        resolved_at=incident.resolved_at,
        age=datetime.now(tz=UTC) - incident.opened_at,
        dedup_count=incident.dedup_count,
        acknowledged=incident.acknowledged,
        tags=list(incident.tags or []),
        last_message=incident.last_message,
    )


def _base_query() -> Select[tuple[Incident, str, str | None, str | None]]:
    return select(
        Incident,
        MonitoredObject.name,
        MonitoredObject.ip,
        MonitoredObject.object_group,
    ).join(MonitoredObject, MonitoredObject.id == Incident.object_id, isouter=True)


@router.get("", response_model=IncidentPage, summary="Список инцидентов")
async def list_incidents(
    status: str | None = Query(default=None, pattern="^(open|resolved)$"),
    severity: int | None = Query(default=None, ge=0, le=5),
    severity_min: int | None = Query(default=None, ge=0, le=5),
    source: str | None = None,
    search: str | None = Query(default=None, max_length=128),
    include_test: bool = False,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> IncidentPage:
    query = _base_query()
    total_query = select(func.count()).select_from(Incident)
    counts_query = (
        select(Incident.severity, func.count())
        .select_from(Incident)
        .join(MonitoredObject, MonitoredObject.id == Incident.object_id, isouter=True)
        .group_by(Incident.severity)
    )

    conditions: list[ColumnElement[bool]] = []
    if not include_test:
        # Тестовые инциденты не попадают в основной дашборд (FR-INC-7)
        conditions.append(Incident.is_test.is_(False))
    if status is not None:
        conditions.append(Incident.status == status)
    if source is not None:
        conditions.append(Incident.source == source)
    if search:
        # Дежурный ищет по тому, что видит: названию правила или имени объекта
        pattern = f"%{search.strip()}%"
        conditions.append(
            Incident.rule_name.ilike(pattern) | MonitoredObject.name.ilike(pattern)
        )

    for condition in conditions:
        query = query.where(condition)
        total_query = total_query.where(condition)
        counts_query = counts_query.where(condition)

    # Отбор по критичности на счётчики не влияет: иначе выбор уровня обнулял бы
    # все остальные плитки, и дежурный терял бы из виду, сколько инцидентов
    # ждёт его на других уровнях.
    # `severity` — ровно этот уровень (так работают плитки: их число обязано
    # совпадать с числом строк), `severity_min` — уровень и выше.
    levels: list[ColumnElement[bool]] = []
    if severity is not None:
        levels.append(Incident.severity == severity)
    if severity_min is not None:
        levels.append(Incident.severity >= severity_min)
    for level in levels:
        query = query.where(level)
        total_query = total_query.where(level)

    if search:
        # Условию нужен объект, поэтому он присоединяется и к запросу счёта
        total_query = total_query.join(
            MonitoredObject, MonitoredObject.id == Incident.object_id, isouter=True
        )

    rows = await session.execute(
        query.order_by(Incident.severity.desc(), Incident.opened_at.desc())
        .limit(size)
        .offset((page - 1) * size)
    )
    total = await session.scalar(total_query)
    counts = {level: 0 for level in range(6)}
    for level, count in (await session.execute(counts_query)).all():
        counts[level] = count

    return IncidentPage(
        items=[_to_item(row) for row in rows],
        total=int(total or 0),
        page=page,
        size=size,
        severity_counts=counts,
    )


@router.get("/{incident_id}", response_model=IncidentCard, summary="Карточка инцидента")
async def get_incident(
    incident_id: int,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> IncidentCard:
    row = (await session.execute(_base_query().where(Incident.id == incident_id))).first()
    if row is None:
        raise ApiError(404, "INCIDENT_NOT_FOUND", "Инцидент не найден")

    incident = row[0]
    item = _to_item(row)

    summary = (
        await session.execute(
            select(LlmSummary)
            .where(LlmSummary.incident_id == incident_id)
            .order_by(LlmSummary.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    events = (
        await session.execute(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.event_ts)
        )
    ).scalars().all()

    return IncidentCard(
        **item.model_dump(),
        rule_key=incident.rule_key,
        rule_expr=incident.rule_expr,
        url=incident.url,
        duration_s=incident.duration_s,
        is_test=incident.is_test,
        summary=(
            IncidentSummary(
                content=summary.content,
                model=summary.model,
                prompt_version=summary.prompt_version,
                masked=summary.masked,
                created_at=summary.created_at,
            )
            if summary
            else None
        ),
        events=[
            IncidentEventItem(
                id=event.id,
                kind=event.kind,
                event_ts=event.event_ts,
                received_at=event.received_at,
                severity=event.severity,
                message=event.message,
                value=event.value,
            )
            for event in events
        ],
    )


@router.get(
    "/{incident_id}/events",
    response_model=list[IncidentEventItem],
    summary="Лента событий инцидента",
)
async def incident_events(
    incident_id: int,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[IncidentEventItem]:
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise ApiError(404, "INCIDENT_NOT_FOUND", "Инцидент не найден")

    events = (
        await session.execute(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.event_ts)
        )
    ).scalars().all()

    return [
        IncidentEventItem(
            id=event.id,
            kind=event.kind,
            event_ts=event.event_ts,
            received_at=event.received_at,
            severity=event.severity,
            message=event.message,
            value=event.value,
        )
        for event in events
    ]


@router.post("/{incident_id}/ack", summary="Квитирование")
async def acknowledge(
    incident_id: int,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Квитирование заложено структурно и по умолчанию выключено (FR-FUT-1).

    При выключенном флаге эндпоинт честно отвечает 501, а не делает вид, что
    инцидент взят в работу. Эскалация по таймауту, ради которой квитирование
    и нужно, в прототип не входит.
    """
    if not get_settings().feature_ack:
        raise ApiError(501, "NOT_IMPLEMENTED", "Квитирование в прототипе не реализовано")

    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise ApiError(404, "INCIDENT_NOT_FOUND", "Инцидент не найден")

    await session.execute(
        update(Incident)
        .where(Incident.id == incident_id, Incident.acknowledged.is_(False))
        .values(acknowledged=True, ack_by=user.id, ack_at=datetime.now(tz=UTC))
    )
    await session.commit()
    return {"status": "acknowledged"}
