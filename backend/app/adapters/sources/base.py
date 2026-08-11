"""Канонический вид события и контракт адаптера источника.

Нормализация — детерминированный код (FR-NORM-6). Языковая модель на этом
слое не применяется: разбор инцидента должен упираться в конкретную строку
правила, а не в «модель так решила».

Новый источник = новый класс, реализующий SourceAdapter, плюс запись в реестре.
Существующие адаптеры при этом не трогаются.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import Field

from app.schemas.base import ApiModel

EventKind = Literal["problem", "resolved", "ack", "update"]

# Нерезолвнутые макросы Zabbix ('{HOSTGROUP.NAME}') и заглушки ('*UNKNOWN*')
# приходят как обычные строки — значением их считать нельзя
_MACRO_RE = re.compile(r"^\{[A-Z0-9_.]+\}$")
_UNKNOWN_VALUES = frozenset({"*UNKNOWN*", ""})

_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<hours>\d{2}):?(?P<minutes>\d{2})$")


class AdapterError(Exception):
    """Событие не удалось привести к каноническому виду."""


class UnknownSeverityError(AdapterError):
    """Уровень критичности отсутствует в severity_map.

    Осознанно приводит к ошибке нормализации: severity_map ведёт администратор,
    и пропуск в нём — конфигурационная дыра. Событие остаётся в raw_events
    и переобрабатывается после исправления таблицы (FR-NORM-5).
    """


def clean(value: Any) -> str | None:
    """Значение или None для пустых строк, '*UNKNOWN*' и нерезолвнутых макросов."""
    if value is None:
        return None
    text = str(value).strip()
    if text in _UNKNOWN_VALUES or _MACRO_RE.match(text):
        return None
    return text


def parse_timezone(value: str) -> tzinfo:
    """Имя зоны IANA ('Europe/Moscow') либо фиксированное смещение ('+03:00')."""
    match = _OFFSET_RE.match(value.strip())
    if match is not None:
        delta = timedelta(
            hours=int(match["hours"]), minutes=int(match["minutes"])
        )
        return timezone(-delta if match["sign"] == "-" else delta)
    if value.strip().upper() == "UTC":
        return UTC
    return ZoneInfo(value.strip())


class SeverityMap:
    """Таблица severity_map: (источник, исходное значение) → шкала 0..5.

    Держится в таблице, а не в коде, потому что источники будут добавляться.
    Сравнение регистронезависимое — так описана колонка raw_value.
    """

    def __init__(self, rows: Mapping[tuple[str, str], int]) -> None:
        self._rows = {(source, raw.lower()): value for (source, raw), value in rows.items()}

    def resolve(self, source: str, raw_value: str | None) -> int:
        if raw_value is None:
            raise UnknownSeverityError(f"{source}: уровень критичности отсутствует в событии")
        try:
            return self._rows[(source, raw_value.strip().lower())]
        except KeyError as exc:
            raise UnknownSeverityError(
                f"{source}: уровень '{raw_value}' отсутствует в severity_map"
            ) from exc


class CanonicalObject(ApiModel):
    """Наблюдаемый объект: без него не построить ни группировку, ни маршрутизацию."""

    external_id: str
    name: str
    ip: str | None = None
    type: str | None = None
    object_group: str | None = None


class CanonicalRule(ApiModel):
    key: str | None = None
    name: str
    expression: str | None = None


class CanonicalEvent(ApiModel):
    """Промежуточный вид, к которому адаптер приводит любой источник.

    Формат зафиксирован в разделе 7 docs/tz.md.
    """

    source_system: str
    source_event_id: str
    external_key: str
    kind: EventKind
    severity: int = Field(ge=0, le=5)
    severity_raw: str
    object: CanonicalObject
    rule: CanonicalRule
    message: str | None = None
    value: str | None = None
    occurred_at: datetime
    tags: list[str] = Field(default_factory=list)
    is_test: bool = False
    attrs: dict[str, Any] = Field(default_factory=dict)


class SourceAdapter(Protocol):
    """Контракт источника. Реализация обязана быть детерминированной."""

    source: str

    def parse(self, payload: dict[str, Any]) -> CanonicalEvent: ...
