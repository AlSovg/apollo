"""Схемы инбокса."""

from datetime import datetime

from app.schemas.base import ApiModel


class InboxItem(ApiModel):
    notification_id: int
    incident_id: int
    severity: int
    rule_name: str
    object_name: str | None
    topic_name: str | None
    summary: str | None
    incident_status: str
    created_at: datetime
    read_at: datetime | None


class InboxPage(ApiModel):
    items: list[InboxItem]
    total: int
    page: int
    size: int
    # Уровень → сколько уведомлений, по всей выборке без фильтра критичности
    severity_counts: dict[int, int]


class UnreadCount(ApiModel):
    unread: int


class DeliveryAttempt(ApiModel):
    """История доставки: канал, статус, попытки, ошибка, время.

    Адреса доставки здесь нет и быть не может — она не хранится (FR-DLV-4).
    """

    notification_id: int
    channel: str
    status: str
    attempts: int
    last_error: str | None
    next_retry_at: datetime | None
    created_at: datetime
    sent_at: datetime | None


class ReadResult(ApiModel):
    updated: int
