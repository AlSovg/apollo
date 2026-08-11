"""Декларативный маппинг полей: источник подключается без кода.

Соответствие полей описывается выражениями в event_sources.field_map, поэтому
подключение системы вроде Prometheus Alertmanager — это заполнение формы
в админке, а не задача разработчика (FR-SRC-2).

Пример для Alertmanager:

    {
      "external_key":       "$.alerts[0].fingerprint",
      "severity_raw":       "$.alerts[0].labels.severity",
      "object.external_id": "$.alerts[0].labels.instance",
      "object.name":        "$.alerts[0].labels.instance",
      "rule.name":          "$.alerts[0].labels.alertname",
      "message":            "$.alerts[0].annotations.summary",
      "occurred_at":        "$.alerts[0].startsAt",
      "kind":               "$.status",
      "kind_map":           {"firing": "problem", "resolved": "resolved"}
    }
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
from app.adapters.sources.jsonpath import PathError, extract

# Ключи field_map, которые не являются выражениями пути
SPECIAL_KEYS = frozenset({"kind_map", "severity_map"})

# Поля, которые можно задать выражением
SUPPORTED_FIELDS = (
    "source_event_id",
    "external_key",
    "kind",
    "severity_raw",
    "message",
    "value",
    "occurred_at",
    "tags",
    "is_test",
    "object.external_id",
    "object.name",
    "object.ip",
    "object.type",
    "object.group",
    "rule.key",
    "rule.name",
    "rule.expression",
)

_RESOLVED_WORDS = frozenset({"resolved", "reset", "ok", "cleared", "recovery"})


def parse_timestamp(raw: str, default_tz: tzinfo) -> datetime:
    """ISO 8601, включая RFC3339 с наносекундами (так шлёт Alertmanager)."""
    text = raw.strip().replace("Z", "+00:00")
    # fromisoformat принимает не больше шести знаков после запятой
    if "." in text:
        head, _, tail = text.partition(".")
        fraction = "".join(ch for ch in tail if ch.isdigit())[:6]
        suffix = tail[len(fraction) :].lstrip("0123456789")
        text = f"{head}.{fraction or '0'}{suffix}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AdapterError(f"не удалось разобрать время '{raw}'") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=default_tz)


def validate_field_map(field_map: dict[str, Any]) -> list[str]:
    """Проверка маппинга до сохранения. Возвращает список проблем."""
    problems: list[str] = []
    for key, value in field_map.items():
        if key in SPECIAL_KEYS:
            if not isinstance(value, dict):
                problems.append(f"'{key}' должен быть объектом соответствий")
            continue
        if key not in SUPPORTED_FIELDS:
            problems.append(f"неизвестное поле '{key}'")
            continue
        if not isinstance(value, str):
            problems.append(f"'{key}': выражение должно быть строкой")
            continue
        try:
            from app.adapters.sources.jsonpath import parse_path

            parse_path(value)
        except PathError as exc:
            problems.append(f"'{key}': {exc}")
    return problems


class MappingAdapter:
    """Приводит произвольный payload к каноническому виду по field_map."""

    def __init__(
        self,
        severity_map: SeverityMap,
        timezone: tzinfo,
        *,
        source: str,
        field_map: dict[str, Any],
    ) -> None:
        self.source = source
        self._severity_map = severity_map
        self._timezone = timezone
        self._field_map = field_map

    def _value(self, payload: dict[str, Any], key: str) -> Any:
        expression = self._field_map.get(key)
        if not isinstance(expression, str):
            return None
        try:
            return extract(payload, expression)
        except PathError as exc:
            # Битое выражение — ошибка нормализации: событие остаётся
            # в raw_events и переобработается после исправления маппинга
            raise AdapterError(f"поле '{key}': {exc}") from exc

    def _text(self, payload: dict[str, Any], key: str) -> str | None:
        return clean(self._value(payload, key))

    def parse(self, payload: dict[str, Any]) -> CanonicalEvent:
        rule_name = self._text(payload, "rule.name") or self._text(payload, "message")
        if rule_name is None:
            raise AdapterError(
                f"{self.source}: маппинг не дал ни 'rule.name', ни 'message'"
            )

        object_id = self._text(payload, "object.external_id") or self._text(
            payload, "object.name"
        )
        if object_id is None:
            raise AdapterError(f"{self.source}: маппинг не дал объекта")

        external_key = (
            self._text(payload, "external_key")
            or self._text(payload, "source_event_id")
            or f"{object_id}|{rule_name}"
        )
        severity_raw = self._text(payload, "severity_raw") or ""

        raw_time = self._text(payload, "occurred_at")
        if raw_time is None:
            raise AdapterError(f"{self.source}: маппинг не дал времени события")

        return CanonicalEvent(
            source_system=self.source,
            source_event_id=self._text(payload, "source_event_id") or external_key,
            external_key=external_key,
            kind=self._kind(payload),
            severity=self._severity_map.resolve(self.source, severity_raw or None),
            severity_raw=severity_raw,
            object=CanonicalObject(
                external_id=object_id,
                name=self._text(payload, "object.name") or object_id,
                ip=self._text(payload, "object.ip"),
                type=self._text(payload, "object.type"),
                object_group=self._text(payload, "object.group"),
            ),
            rule=CanonicalRule(
                key=self._text(payload, "rule.key"),
                name=rule_name,
                expression=self._text(payload, "rule.expression"),
            ),
            message=self._text(payload, "message"),
            value=self._text(payload, "value"),
            occurred_at=parse_timestamp(raw_time, self._timezone),
            tags=self._tags(payload),
            is_test=bool(self._value(payload, "is_test") or False),
            attrs={},
        )

    def _kind(self, payload: dict[str, Any]) -> EventKind:
        raw = self._text(payload, "kind")
        if raw is None:
            return "problem"

        kind_map = self._field_map.get("kind_map")
        if isinstance(kind_map, dict):
            mapped = kind_map.get(raw) or kind_map.get(raw.lower())
            for known in ("problem", "resolved", "ack", "update"):
                if mapped == known:
                    return known

        # Без таблицы соответствий полагаемся на общепринятые слова
        return "resolved" if raw.lower() in _RESOLVED_WORDS else "problem"

    def _tags(self, payload: dict[str, Any]) -> list[str]:
        raw = self._value(payload, "tags")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [item.strip() for item in raw.split(",") if item.strip()]
        if isinstance(raw, dict):
            return [f"{key}:{value}" for key, value in raw.items()]
        if isinstance(raw, list):
            return [str(item) for item in raw if item is not None]
        return []
