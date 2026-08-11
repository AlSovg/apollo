"""Реестр адаптеров: разрешение по записи в event_sources.

Три уровня подключения источника (FR-SRC), от самого дешёвого:

  kind=canonical — система шлёт готовый канонический JSON, кода не нужно
  kind=mapping   — соответствие полей описано в field_map, кода не нужно
  kind=adapter   — преобразование не выражается маппингом, нужен класс

Появление третьего источника не должно трогать существующие адаптеры и ядро
(раздел 9 AGENTS.md).
"""

from collections.abc import Callable
from datetime import UTC, tzinfo
from typing import Any, Protocol

from app.adapters.sources.base import SeverityMap, SourceAdapter, parse_timezone
from app.adapters.sources.generic import GenericAdapter
from app.adapters.sources.mapping import MappingAdapter
from app.adapters.sources.solarwinds import SolarWindsAdapter
from app.adapters.sources.zabbix import ZabbixAdapter

AdapterFactory = Callable[[SeverityMap, tzinfo], SourceAdapter]

# Имя класса из event_sources.adapter_class → фабрика
ADAPTERS: dict[str, AdapterFactory] = {
    "ZabbixAdapter": ZabbixAdapter,
    "SolarWindsAdapter": SolarWindsAdapter,
}


class SourceRecord(Protocol):
    """То, что нужно от записи реестра. Модель БД сюда подходит как есть."""

    code: str
    kind: str
    adapter_class: str | None
    field_map: dict[str, Any]
    timezone: str | None


class UnknownAdapterError(Exception):
    """В реестре нет адаптера с таким именем или запись описана неверно."""


def resolve_timezone(configured: str | None, fallback: str | None = None) -> tzinfo:
    """Таймзона источника: из реестра источников, иначе из настроек, иначе UTC.

    Ошибка здесь смещает все инциденты источника на N часов, поэтому значение
    берётся явно, а не подразумевается.
    """
    for candidate in (configured, fallback):
        if candidate:
            return parse_timezone(candidate)
    return UTC


def build_adapter(
    adapter_class: str | None, severity_map: SeverityMap, timezone: tzinfo
) -> SourceAdapter:
    if adapter_class is None:
        raise UnknownAdapterError("У источника не указан класс адаптера")
    try:
        factory = ADAPTERS[adapter_class]
    except KeyError as exc:
        raise UnknownAdapterError(f"Адаптер '{adapter_class}' не зарегистрирован") from exc
    return factory(severity_map, timezone)


def adapter_for_source(
    source: SourceRecord, severity_map: SeverityMap, timezone: tzinfo
) -> SourceAdapter:
    """Адаптер по записи реестра — единственная точка разрешения."""
    if source.kind == "canonical":
        return GenericAdapter(severity_map, timezone, source=source.code)
    if source.kind == "mapping":
        if not source.field_map:
            raise UnknownAdapterError(
                f"У источника '{source.code}' пустой маппинг полей"
            )
        return MappingAdapter(
            severity_map, timezone, source=source.code, field_map=source.field_map
        )
    if source.kind == "adapter":
        return build_adapter(source.adapter_class, severity_map, timezone)
    raise UnknownAdapterError(f"Неизвестный способ подключения: '{source.kind}'")
