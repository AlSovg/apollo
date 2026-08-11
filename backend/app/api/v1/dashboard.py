"""Дашборд администратора: состояние платформы, а не инцидентов.

Не путать с дашбордом инцидентов: здесь нагрузка, эффективность фильтрации,
доставка и задержки конвейера. Доступ — только роль admin.
"""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.config import get_settings
from app.db import get_session
from app.schemas.dashboard import (
    ChannelResponse,
    DeliveryResponse,
    FailureResponse,
    IntegrationHealthResponse,
    IntegrationsResponse,
    LatencyResponse,
    LatencySegmentResponse,
    LoadPointResponse,
    LoadResponse,
    ProcessingResponse,
    SourceHealthResponse,
    TopEntryResponse,
    TopResponse,
)
from app.services.dashboard import (
    delivery_stats,
    latency_stats,
    load_stats,
    period_start,
    processing_stats,
    source_health,
    top_stats,
)
from app.services.health_checks import check_integrations

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/dashboard", tags=["admin"])

Period = Query(default="24h", pattern="^(1h|24h|7d|30d)$")


@router.get("/load", response_model=LoadResponse, summary="Нагрузка и очереди")
async def load(
    period: str = Period,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> LoadResponse:
    stats = await load_stats(session, period_start(period))
    return LoadResponse(
        period=period,
        events_total=stats.events_total,
        events_per_minute=[
            LoadPointResponse(bucket=point.bucket, source=point.source, events=point.events)
            for point in stats.events_per_minute
        ],
        normalization_backlog=stats.normalization_backlog,
        delivery_backlog=stats.delivery_backlog,
    )


@router.get(
    "/processing",
    response_model=ProcessingResponse,
    summary="Обработка и коэффициент дедупликации",
)
async def processing(
    period: str = Period,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> ProcessingResponse:
    """Главная цифра защиты: во сколько раз поток событий сжался в инциденты."""
    stats = await processing_stats(session, period_start(period))
    return ProcessingResponse(
        period=period,
        events_received=stats.events_received,
        events_normalized=stats.events_normalized,
        normalization_errors=stats.normalization_errors,
        incidents_created=stats.incidents_created,
        suppressed_repeats=stats.suppressed_repeats,
        dedup_ratio=stats.dedup_ratio,
        noise_reduction=stats.noise_reduction,
    )


@router.get("/delivery", response_model=DeliveryResponse, summary="Доставка по каналам")
async def delivery(
    period: str = Period,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> DeliveryResponse:
    stats = await delivery_stats(session, period_start(period))
    return DeliveryResponse(
        period=period,
        sent=stats.sent,
        failed=stats.failed,
        pending=stats.pending,
        skipped=stats.skipped,
        delivered_percent=stats.delivered_percent,
        by_channel=[
            ChannelResponse(
                channel=item.channel,
                sent=item.sent,
                failed=item.failed,
                pending=item.pending,
                skipped=item.skipped,
            )
            for item in stats.by_channel
        ],
        failures=[
            FailureResponse(reason=item.reason, count=item.count) for item in stats.failures
        ],
    )


@router.get("/top", response_model=TopResponse, summary="Топ правил и объектов")
async def top(
    period: str = Period,
    limit: int = Query(default=10, ge=1, le=50),
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> TopResponse:
    stats = await top_stats(session, period_start(period), limit=limit)
    return TopResponse(
        period=period,
        rules=[
            TopEntryResponse(name=item.name, incidents=item.incidents, events=item.events)
            for item in stats.rules
        ],
        objects=[
            TopEntryResponse(name=item.name, incidents=item.incidents, events=item.events)
            for item in stats.objects
        ],
    )


@router.get("/latency", response_model=LatencyResponse, summary="Задержки конвейера")
async def latency(
    period: str = Period,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> LatencyResponse:
    stats = await latency_stats(session, period_start(period))
    return LatencyResponse(
        period=period,
        segments=[
            LatencySegmentResponse(
                segment=item.segment,
                samples=item.samples,
                p50_ms=item.p50_ms,
                p95_ms=item.p95_ms,
                p99_ms=item.p99_ms,
                target_ms=item.target_ms,
            )
            for item in stats.segments
        ],
    )


@router.get(
    "/integrations", response_model=IntegrationsResponse, summary="Состояние интеграций"
)
async def integrations(
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> IntegrationsResponse:
    """Молчащий источник и лежащая интеграция должны быть видны сразу."""
    sources = await source_health(session)
    checks = await check_integrations(get_settings())

    return IntegrationsResponse(
        sources=[
            SourceHealthResponse(
                code=item.code,
                name=item.name,
                is_active=item.is_active,
                last_event_at=item.last_event_at,
                silent_minutes=item.silent_minutes,
            )
            for item in sources
        ],
        integrations=[
            IntegrationHealthResponse(name=item.name, status=item.status, detail=item.detail)
            for item in checks
        ],
    )
