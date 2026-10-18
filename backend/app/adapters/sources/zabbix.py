"""Адаптер Zabbix Server 7.0.

Поля вебхука перечислены в «local/ФАЙЛИК ДЛЯ САШИ.md» (раздел 3.4) и соответствуют
media type «JSON Webhook Receiver», настроенному на стенде.
"""

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

# Группа тестовых хостов на стенде: события оттуда не попадают в дашборд (FR-INC-7)
TEST_HOST_GROUP = "Test webhook group"

# Поля, которые легли в канонические колонки. Всё остальное уходит в attrs —
# ничего не выбрасываем (item_key, item_name, opdata и прочее).
_CONSUMED_FIELDS = frozenset(
    {
        "event_id",
        "event_nseverity",
        "severity",
        "status",
        "event_value",
        "timestamp",
        "date",
        "time",
        "host",
        "host_technical_name",
        "host_ip",
        "hostgroup",
        "trigger_id",
        "trigger_name",
        "trigger_expression",
        "message",
        "item_lastvalue",
        "tags",
        "event_tags",
    }
)

_RESOLVED_STATUSES = frozenset({"RESOLVED", "OK"})


def _parse_tags(payload: dict[str, Any]) -> list[str]:
    """Теги приходят списком строк, списком пар или строкой через запятую."""
    raw = payload.get("event_tags", payload.get("tags"))
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if not isinstance(raw, list):
        return []

    tags: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            tag, value = clean(item.get("tag")), clean(item.get("value"))
            if tag is None:
                continue
            tags.append(f"{tag}:{value}" if value else tag)
        else:
            text = clean(item)
            if text is not None:
                tags.append(text)
    return tags


class ZabbixAdapter:
    """Zabbix → канонический вид."""

    source = "zabbix"

    def __init__(self, severity_map: SeverityMap, timezone: tzinfo) -> None:
        self._severity_map = severity_map
        # Zabbix отдаёт timestamp/date/time БЕЗ таймзоны — это локальное время
        # сервера Zabbix. Захардкоженный UTC сместил бы все инциденты на N часов.
        self._timezone = timezone

    def parse(self, payload: dict[str, Any]) -> CanonicalEvent:
        event_id = clean(payload.get("event_id"))
        if event_id is None:
            raise AdapterError("zabbix: в событии нет event_id")

        host_technical_name = clean(payload.get("host_technical_name")) or clean(
            payload.get("host")
        )
        if host_technical_name is None:
            raise AdapterError("zabbix: в событии нет хоста")

        trigger_name = clean(payload.get("trigger_name")) or clean(
            payload.get("event_name")
        )
        if trigger_name is None:
            raise AdapterError("zabbix: в событии нет имени триггера")

        severity_raw = clean(payload.get("severity")) or ""
        # Числовое event_nseverity надёжнее строки: локализация и переименование
        # уровней в интерфейсе Zabbix на него не влияют
        severity = self._severity_map.resolve(
            self.source, clean(payload.get("event_nseverity"))
        )

        hostgroup = clean(payload.get("hostgroup"))

        return CanonicalEvent(
            source_system=self.source,
            source_event_id=event_id,
            external_key=event_id,
            kind=self._kind(payload),
            severity=severity,
            severity_raw=severity_raw,
            object=CanonicalObject(
                external_id=host_technical_name,
                name=clean(payload.get("host")) or host_technical_name,
                ip=clean(payload.get("host_ip")),  # '*UNKNOWN*' превращается в None
                type="host",
                object_group=hostgroup,
            ),
            rule=CanonicalRule(
                key=clean(payload.get("trigger_id")),
                name=trigger_name,
                expression=clean(payload.get("trigger_expression")),
            ),
            message=clean(payload.get("message")),
            value=clean(payload.get("item_lastvalue")),
            occurred_at=self._occurred_at(payload),
            tags=_parse_tags(payload),
            is_test=hostgroup == TEST_HOST_GROUP,
            attrs={
                key: value
                for key, value in payload.items()
                if key not in _CONSUMED_FIELDS
            },
        )

    def _kind(self, payload: dict[str, Any]) -> EventKind:
        status = (clean(payload.get("status")) or "").upper()
        if status in _RESOLVED_STATUSES:
            return "resolved"
        if status == "PROBLEM":
            return "problem"
        # event_value: '1' — проблема, '0' — восстановление
        return "resolved" if clean(payload.get("event_value")) == "0" else "problem"

    def _occurred_at(self, payload: dict[str, Any]) -> datetime:
        """timestamp вида '2026.08.10 10:53:52' либо пара date + time."""
        raw = clean(payload.get("timestamp"))
        if raw is None:
            date_part, time_part = clean(payload.get("date")), clean(payload.get("time"))
            if date_part is None or time_part is None:
                raise AdapterError("zabbix: в событии нет времени")
            raw = f"{date_part} {time_part}"

        for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
            try:
                naive = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            return naive.replace(tzinfo=self._timezone)

        raise AdapterError(f"zabbix: не удалось разобрать время '{raw}'")
