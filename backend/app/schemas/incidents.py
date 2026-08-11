"""Схемы инцидентов."""

from datetime import datetime, timedelta

from app.schemas.base import ApiModel


class IncidentSummary(ApiModel):
    content: str
    model: str
    prompt_version: str
    masked: bool
    created_at: datetime


class IncidentEventItem(ApiModel):
    id: int
    kind: str
    event_ts: datetime
    received_at: datetime
    severity: int | None
    message: str | None
    value: str | None


class IncidentListItem(ApiModel):
    id: int
    source: str
    external_key: str
    rule_name: str
    severity: int
    severity_raw: str
    status: str
    object_name: str | None
    object_ip: str | None
    object_group: str | None
    opened_at: datetime
    resolved_at: datetime | None
    age: timedelta
    dedup_count: int
    acknowledged: bool
    tags: list[str]
    last_message: str | None


class IncidentPage(ApiModel):
    items: list[IncidentListItem]
    total: int
    page: int
    size: int
    # Разбивка по уровням считается по всей отфильтрованной выборке, а не по
    # странице: иначе счётчики в шапке показывали бы только текущие 50 строк
    severity_counts: dict[int, int]


class IncidentCard(IncidentListItem):
    rule_key: str | None
    rule_expr: str | None
    url: str | None
    duration_s: int | None
    is_test: bool
    summary: IncidentSummary | None
    events: list[IncidentEventItem]
