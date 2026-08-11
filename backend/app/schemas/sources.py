"""Схемы реестра источников.

Секреты наружу не отдаются никогда: в ответах только признаки «токен задан»
и «секрет подписи задан».
"""

from datetime import datetime
from typing import Any

from pydantic import Field

from app.adapters.sources.base import CanonicalEvent
from app.schemas.base import ApiModel


class SourceCreate(ApiModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    name: str = Field(min_length=1, max_length=256)
    kind: str = Field(pattern="^(adapter|mapping|canonical)$")
    adapter_class: str | None = None
    field_map: dict[str, Any] = Field(default_factory=dict)
    timezone: str | None = None
    ip_allowlist: list[str] = Field(default_factory=list)


class SourceUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    kind: str | None = Field(default=None, pattern="^(adapter|mapping|canonical)$")
    adapter_class: str | None = None
    field_map: dict[str, Any] | None = None
    timezone: str | None = None
    ip_allowlist: list[str] | None = None
    is_active: bool | None = None


class SourceResponse(ApiModel):
    code: str
    name: str
    kind: str
    adapter_class: str | None
    field_map: dict[str, Any]
    timezone: str | None
    ip_allowlist: list[str]
    is_active: bool
    # Сами значения не показываем — только факт их наличия
    token_set: bool
    hmac_set: bool
    last_event_at: datetime | None
    created_at: datetime


class TokenIssued(ApiModel):
    """Токен показывается ровно один раз — в БД лежит только его хеш."""

    code: str
    token: str
    hmac_secret: str | None = None


class SourceTestRequest(ApiModel):
    payload: dict[str, Any]
    # Маппинг можно проверить до сохранения: подбирать вслепую не придётся
    field_map: dict[str, Any] | None = None


class SourceTestResult(ApiModel):
    ok: bool
    event: CanonicalEvent | None = None
    error: str | None = None
    problems: list[str] = Field(default_factory=list)
