"""Универсальный приём: канонический формат и декларативный маппинг."""

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.sources import (
    AdapterError,
    GenericAdapter,
    MappingAdapter,
    SeverityMap,
    UnknownAdapterError,
    adapter_for_source,
    validate_field_map,
)
from app.adapters.sources.jsonpath import PathError, extract, parse_path
from app.adapters.sources.mapping import parse_timestamp
from app.models.events import EventSource
from tests.conftest import load_fixture

SEVERITY = SeverityMap(
    {
        ("generic", "high"): 4,
        ("prometheus", "critical"): 4,
        ("prometheus", "warning"): 2,
        ("zabbix", "4"): 4,
    }
)

# Маппинг Alertmanager — то, что администратор заполняет в форме
ALERTMANAGER_MAP = {
    "external_key": "$.alerts[0].fingerprint",
    "severity_raw": "$.alerts[0].labels.severity",
    "object.external_id": "$.alerts[0].labels.instance",
    "object.name": "$.alerts[0].labels.instance",
    "rule.name": "$.alerts[0].labels.alertname",
    "message": "$.alerts[0].annotations.summary",
    "rule.expression": "$.alerts[0].annotations.description",
    "occurred_at": "$.alerts[0].startsAt",
    "kind": "$.status",
    "tags": "$.commonLabels",
    "kind_map": {"firing": "problem", "resolved": "resolved"},
}

CANONICAL_PAYLOAD = {
    "source_system": "generic",
    "source_event_id": "8841203",
    "external_key": "8841203",
    "kind": "problem",
    "severity": 4,
    "severity_raw": "High",
    "object": {"external_id": "db-prod-03", "name": "db-prod-03", "ip": "10.0.3.14"},
    "rule": {"key": "25239", "name": "Диск заполнен на 94%"},
    "message": "Диск заполнен на 94%",
    "occurred_at": "2026-07-10T02:14:00Z",
    "tags": ["disk", "prod"],
}


# --- выражения путей --------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("$.status", "firing"),
        ("status", "firing"),
        ("$.alerts[0].labels.severity", "critical"),
        ("$.alerts[0].fingerprint", "a1b2c3d4e5f60718"),
        ("$.alerts[-1].labels.alertname", "HostOutOfMemory"),
        ("$.alerts[5].labels.severity", None),  # индекса нет — не ошибка
        ("$.нет.такого.поля", None),
    ],
)
def test_path_extraction(expression: str, expected: object) -> None:
    assert extract(load_fixture("prometheus_alertmanager.json"), expression) == expected


@pytest.mark.parametrize(
    "expression",
    ["$.alerts[*].labels", "$..severity", "$.alerts[?(@.status=='firing')]", "$.a[x]", ""],
)
def test_unsupported_syntax_fails_loudly(expression: str) -> None:
    """Неподдержанный синтаксис должен дать ошибку, а не молчаливый None."""
    with pytest.raises(PathError):
        parse_path(expression)


def test_path_steps() -> None:
    assert parse_path("$.alerts[0].labels.severity") == ["alerts", 0, "labels", "severity"]


# --- маппинг ---------------------------------------------------------------


def test_alertmanager_payload_becomes_canonical_event() -> None:
    """Ключевая проверка шага: чужой формат разбирается без кода."""
    adapter = MappingAdapter(
        SEVERITY, UTC, source="prometheus", field_map=dict(ALERTMANAGER_MAP)
    )

    event = adapter.parse(load_fixture("prometheus_alertmanager.json"))

    assert event.source_system == "prometheus"
    assert event.external_key == "a1b2c3d4e5f60718"
    assert event.kind == "problem"
    assert event.severity == 4
    assert event.severity_raw == "critical"
    assert event.object.external_id == "app-prod-07:9100"
    assert event.rule.name == "HostOutOfMemory"
    assert event.message == "Свободной памяти меньше 10% на app-prod-07"
    assert event.occurred_at == datetime(
        2026, 8, 10, 12, 15, 3, 481920, tzinfo=UTC
    )
    assert event.tags == ["severity:critical"]


def test_kind_map_translates_resolution() -> None:
    payload = load_fixture("prometheus_alertmanager.json")
    payload["status"] = "resolved"
    adapter = MappingAdapter(
        SEVERITY, UTC, source="prometheus", field_map=dict(ALERTMANAGER_MAP)
    )

    assert adapter.parse(payload).kind == "resolved"


def test_broken_expression_is_reported_as_adapter_error() -> None:
    """Битое выражение — ошибка нормализации, событие не теряется."""
    adapter = MappingAdapter(
        SEVERITY,
        UTC,
        source="prometheus",
        field_map={**ALERTMANAGER_MAP, "message": "$.alerts[*].annotations.summary"},
    )

    with pytest.raises(AdapterError):
        adapter.parse(load_fixture("prometheus_alertmanager.json"))


def test_mapping_without_object_fails_clearly() -> None:
    adapter = MappingAdapter(
        SEVERITY, UTC, source="prometheus", field_map={"rule.name": "$.status"}
    )

    with pytest.raises(AdapterError, match="объекта"):
        adapter.parse(load_fixture("prometheus_alertmanager.json"))


def test_field_map_validation_catches_typos() -> None:
    problems = validate_field_map(
        {"externall_key": "$.a", "message": "$.alerts[*].x", "kind_map": "не объект"}
    )

    assert any("externall_key" in problem for problem in problems)
    assert any("message" in problem for problem in problems)
    assert any("kind_map" in problem for problem in problems)


def test_valid_field_map_has_no_problems() -> None:
    assert validate_field_map(ALERTMANAGER_MAP) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-10T12:15:03Z", datetime(2026, 8, 10, 12, 15, 3, tzinfo=UTC)),
        # Alertmanager шлёт наносекунды, а fromisoformat берёт максимум шесть знаков
        ("2026-08-10T12:15:03.481920123Z", datetime(2026, 8, 10, 12, 15, 3, 481920, tzinfo=UTC)),
        ("2026-08-10T15:15:03+03:00", datetime(2026, 8, 10, 12, 15, 3, tzinfo=UTC)),
    ],
)
def test_timestamp_parsing(raw: str, expected: datetime) -> None:
    assert parse_timestamp(raw, UTC).astimezone(UTC) == expected


def test_naive_timestamp_uses_source_timezone() -> None:
    from app.adapters.sources.base import parse_timezone

    parsed = parse_timestamp("2026-08-10T12:15:03", parse_timezone("+03:00"))

    assert parsed.utcoffset() == timedelta(hours=3)


# --- канонический приём -----------------------------------------------------


def test_canonical_payload_needs_no_adapter() -> None:
    adapter = GenericAdapter(SEVERITY, UTC, source="generic")

    event = adapter.parse(CANONICAL_PAYLOAD)

    assert event.external_key == "8841203"
    assert event.severity == 4
    assert event.object.external_id == "db-prod-03"
    assert event.rule.name == "Диск заполнен на 94%"


def test_source_system_cannot_be_forged() -> None:
    """Источником считается тот, чьим токеном подписан запрос."""
    adapter = GenericAdapter(SEVERITY, UTC, source="generic")

    event = adapter.parse({**CANONICAL_PAYLOAD, "source_system": "zabbix"})

    assert event.source_system == "generic"


def test_broken_canonical_payload_explains_what_is_missing() -> None:
    adapter = GenericAdapter(SEVERITY, UTC, source="generic")
    payload = {key: value for key, value in CANONICAL_PAYLOAD.items() if key != "rule"}

    with pytest.raises(AdapterError, match="rule"):
        adapter.parse(payload)


# --- разрешение по записи реестра ------------------------------------------


def _source(**kwargs: object) -> EventSource:
    defaults: dict[str, object] = {
        "code": "prometheus",
        "name": "Prometheus",
        "kind": "mapping",
        "adapter_class": None,
        "field_map": dict(ALERTMANAGER_MAP),
        "timezone": None,
    }
    defaults.update(kwargs)
    return EventSource(**defaults)


def test_registry_resolves_all_three_kinds() -> None:
    mapping = adapter_for_source(_source(), SEVERITY, UTC)
    canonical = adapter_for_source(
        _source(code="generic", kind="canonical", field_map={}), SEVERITY, UTC
    )
    coded = adapter_for_source(
        _source(code="zabbix", kind="adapter", adapter_class="ZabbixAdapter", field_map={}),
        SEVERITY,
        UTC,
    )

    assert isinstance(mapping, MappingAdapter)
    assert isinstance(canonical, GenericAdapter)
    assert coded.source == "zabbix"


def test_mapping_source_without_field_map_is_rejected() -> None:
    with pytest.raises(UnknownAdapterError):
        adapter_for_source(_source(field_map={}), SEVERITY, UTC)


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(UnknownAdapterError):
        adapter_for_source(_source(kind="волшебство"), SEVERITY, UTC)
