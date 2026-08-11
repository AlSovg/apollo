"""Схемы административного дашборда платформы."""

from datetime import datetime

from app.schemas.base import ApiModel


class LoadPointResponse(ApiModel):
    bucket: datetime
    source: str
    events: int


class LoadResponse(ApiModel):
    period: str
    events_total: int
    events_per_minute: list[LoadPointResponse]
    normalization_backlog: int
    delivery_backlog: int


class ProcessingResponse(ApiModel):
    period: str
    events_received: int
    events_normalized: int
    normalization_errors: int
    incidents_created: int
    suppressed_repeats: int
    # 1 − инцидентов/событий: доля событий, не потребовавших отдельного разбора
    dedup_ratio: float
    # Во сколько раз поток событий сжался в инциденты
    noise_reduction: float


class ChannelResponse(ApiModel):
    channel: str
    sent: int
    failed: int
    pending: int
    skipped: int


class FailureResponse(ApiModel):
    reason: str
    count: int


class DeliveryResponse(ApiModel):
    period: str
    sent: int
    failed: int
    pending: int
    skipped: int
    delivered_percent: float
    by_channel: list[ChannelResponse]
    failures: list[FailureResponse]


class TopEntryResponse(ApiModel):
    name: str
    incidents: int
    events: int


class TopResponse(ApiModel):
    period: str
    rules: list[TopEntryResponse]
    objects: list[TopEntryResponse]


class LatencySegmentResponse(ApiModel):
    segment: str
    samples: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    # Норматив участка, если он задан требованиями; сравнение — по p95
    target_ms: int | None = None


class LatencyResponse(ApiModel):
    period: str
    segments: list[LatencySegmentResponse]


class SourceHealthResponse(ApiModel):
    code: str
    name: str
    is_active: bool
    last_event_at: datetime | None
    silent_minutes: float | None


class IntegrationHealthResponse(ApiModel):
    name: str
    status: str
    detail: str | None


class IntegrationsResponse(ApiModel):
    sources: list[SourceHealthResponse]
    integrations: list[IntegrationHealthResponse]
