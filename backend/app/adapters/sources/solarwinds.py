"""Адаптер SolarWinds Hybrid Cloud Observability.

Источник присылает события в двух видах, и оба здесь поддержаны:

1. Вложенный JSON (`alert`, `node`) — так описано соответствие полей
   в разделе 7.1 docs/tz.md.
2. Плоский набор query-параметров — так фактически настроено действие
   «Send a GET/POST Request» на стенде (`ФАЙЛИК ДЛЯ САШИ.md`).

Разница спрятана в функции `_pick`, ядро её не видит.
"""

import re
from datetime import datetime, tzinfo
from typing import Any

from app.adapters.sources.base import (
    AdapterError,
    CanonicalEvent,
    CanonicalObject,
    CanonicalRule,
    EventKind,
    SeverityMap,
    clean,
)

# Числового значения метрики отдельным полем нет — оно внутри текста сообщения.
# Извлекаем регуляркой, но при неудаче не падаем: value остаётся None.
_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")

# TODO(открытый вопрос 1 в docs/tz.md): формат reset-события не подтверждён —
# реального срабатывания на стенде не наблюдали. Гипотеза ниже проверяется
# первым же настоящим закрытием алерта.
_RESOLVED_EVENT_TYPES = frozenset({"reset", "resolved", "cleared", "recovery"})

_CONSUMED_FIELDS = frozenset(
    {
        "alert",
        "node",
        "meta",
        "eventType",
        "timestamp",
        "name",
        "severity",
        "status",
        "objectid",
        "objectId",
        "nodeid",
        "nodeId",
        "ip",
        "message",
        "time",
    }
)


def _pick(payload: dict[str, Any], *paths: str) -> Any:
    """Первое найденное значение по путям вида 'alert.objectId' или 'name'."""
    for path in paths:
        current: Any = payload
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None:
            return current
    return None


def _parse_time(raw: str) -> datetime | None:
    """ISO либо формат Windows-локали: '8/10/2026 12:42:10 PM'."""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


class SolarWindsAdapter:
    """SolarWinds → канонический вид."""

    source = "solarwinds"

    def __init__(self, severity_map: SeverityMap, timezone: tzinfo) -> None:
        self._severity_map = severity_map
        # Время в payload без таймзоны — берём настроенную у источника
        self._timezone = timezone

    def parse(self, payload: dict[str, Any]) -> CanonicalEvent:
        alert_name = clean(_pick(payload, "alert.name", "name", "alertName"))
        if alert_name is None:
            raise AdapterError("solarwinds: в событии нет имени алерта")

        node_name = clean(_pick(payload, "node.name", "node", "nodeName", "caption"))
        node_id = clean(
            _pick(payload, "alert.objectId", "node.objectId", "objectid", "nodeid")
        )
        if node_name is None and node_id is None:
            raise AdapterError("solarwinds: в событии нет узла")

        # objectId — идентификатор УЗЛА, поэтому в ключ добавляется имя правила:
        # два разных алерта на одном узле иначе затрут друг друга.
        # На стенде objectId пока не передаётся (см. tests/fixtures/README.md),
        # тогда роль идентификатора играет имя узла.
        object_external_id = node_id or node_name
        assert object_external_id is not None  # проверено выше
        external_key = f"{object_external_id}|{alert_name}"

        severity_raw = clean(_pick(payload, "alert.severity", "severity")) or ""
        severity = self._severity_map.resolve(self.source, severity_raw or None)

        message = clean(_pick(payload, "alert.message", "message"))

        return CanonicalEvent(
            source_system=self.source,
            source_event_id=object_external_id,
            external_key=external_key,
            kind=self._kind(payload),
            severity=severity,
            severity_raw=severity_raw,
            object=CanonicalObject(
                external_id=object_external_id,
                name=node_name or object_external_id,
                ip=clean(_pick(payload, "node.ipAddress", "ip")),
                type="node",
                object_group=clean(_pick(payload, "node.group", "group")),
            ),
            rule=CanonicalRule(
                key=alert_name,
                name=alert_name,
                expression=clean(_pick(payload, "alert.description", "description")),
            ),
            message=message,
            value=self._value(message),
            occurred_at=self._occurred_at(payload),
            tags=[],
            is_test=bool(_pick(payload, "meta.testEvent") or False),
            attrs={
                key: value
                for key, value in payload.items()
                if key not in _CONSUMED_FIELDS
            },
        )

    def _kind(self, payload: dict[str, Any]) -> EventKind:
        event_type = (clean(_pick(payload, "eventType", "status", "alert.status")) or "").lower()
        if event_type in _RESOLVED_EVENT_TYPES:
            return "resolved"
        return "problem"

    def _value(self, message: str | None) -> str | None:
        if message is None:
            return None
        match = _VALUE_RE.search(message)
        return match.group(1).replace(",", ".") if match else None

    def _occurred_at(self, payload: dict[str, Any]) -> datetime:
        raw = clean(
            _pick(payload, "timestamp", "time", "alert.triggerTime", "triggerTime")
        )
        if raw is None:
            raise AdapterError("solarwinds: в событии нет времени")

        parsed = _parse_time(raw)
        if parsed is None:
            raise AdapterError(f"solarwinds: не удалось разобрать время '{raw}'")
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=self._timezone)
