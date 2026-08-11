"""Схемы ответов приёма событий."""

from app.schemas.base import ApiModel


class IngestAccepted(ApiModel):
    status: str
    raw_event_id: int
