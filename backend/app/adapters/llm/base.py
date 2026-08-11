"""Контракт языковой модели.

Единственный сценарий применения модели в системе — саммаризация инцидента
(раздел 3.7 docs/tz.md). Нормализация, дедупликация и определение критичности
остаются детерминированным кодом: разбор «почему уведомление ушло не туда»
должен упираться в строку правила, а не в «модель так решила».

Модель работает внутри контура. Внешние API не подключаются ни при каких
условиях (раздел 2 AGENTS.md).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class LlmError(Exception):
    """Базовая ошибка обращения к модели."""


class LlmUnavailableError(LlmError):
    """Модель недоступна или не уложилась в таймаут.

    Доставку это не блокирует: уведомление уходит без сводки, а сводка
    подтягивается позже (FR-AI-4).
    """


@dataclass
class IncidentEventLine:
    kind: str
    event_ts: datetime
    message: str | None = None
    value: str | None = None


@dataclass
class IncidentContext:
    """Всё, что модель знает об инциденте. Ничего сверх этого выдумывать нельзя."""

    incident_id: int
    source: str
    severity: int
    severity_raw: str
    status: str
    rule_name: str
    rule_expression: str | None = None
    object_name: str | None = None
    object_ip: str | None = None
    object_group: str | None = None
    message: str | None = None
    value: str | None = None
    tags: list[str] = field(default_factory=list)
    dedup_count: int = 1
    opened_at: datetime | None = None
    events: list[IncidentEventLine] = field(default_factory=list)


@dataclass
class LlmResult:
    content: str
    model: str
    latency_ms: int


class LlmClient(Protocol):
    async def summarize(self, prompt: str, context: str) -> LlmResult: ...
