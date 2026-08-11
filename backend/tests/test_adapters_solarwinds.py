"""Адаптер SolarWinds: плоский формат стенда и вложенный формат из ТЗ."""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.adapters.sources import AdapterError, SeverityMap, SolarWindsAdapter
from app.adapters.sources.base import parse_timezone
from tests.conftest import load_fixture

MOSCOW = parse_timezone("Europe/Moscow")

SEVERITY_MAP = SeverityMap(
    {
        ("solarwinds", "informational"): 0,
        ("solarwinds", "notice"): 1,
        ("solarwinds", "warning"): 2,
        ("solarwinds", "serious"): 3,
        ("solarwinds", "critical"): 4,
    }
)

# Вложенный вид из раздела 7.1 docs/tz.md
NESTED_PAYLOAD: dict[str, Any] = {
    "eventType": "trigger",
    "timestamp": "2026-08-10T12:42:10Z",
    "alert": {
        "objectId": "1042",
        "name": "High CPU Utilization",
        "severity": "Critical",
        "message": "Node WINDOWS-STD3-80 CPU utilization is critical (94%)",
        "description": "CPU load above threshold",
    },
    "node": {
        "objectId": "1042",
        "name": "WINDOWS-STD3-80",
        "ipAddress": "83.166.251.15",
        "machineType": "Windows Server 2022",
    },
}


@pytest.fixture
def adapter() -> SolarWindsAdapter:
    return SolarWindsAdapter(SEVERITY_MAP, MOSCOW)


def test_nested_payload_maps_every_field(adapter: SolarWindsAdapter) -> None:
    event = adapter.parse(NESTED_PAYLOAD)

    assert event.source_system == "solarwinds"
    assert event.kind == "problem"
    assert event.severity == 4
    assert event.severity_raw == "Critical"
    # objectId — идентификатор УЗЛА, поэтому в ключе обязано быть имя правила
    assert event.external_key == "1042|High CPU Utilization"
    assert event.object.external_id == "1042"
    assert event.object.name == "WINDOWS-STD3-80"
    assert event.object.ip == "83.166.251.15"
    assert event.rule.name == "High CPU Utilization"
    assert event.rule.expression == "CPU load above threshold"
    assert event.occurred_at.astimezone(UTC) == datetime(2026, 8, 10, 12, 42, 10, tzinfo=UTC)


def test_two_rules_on_one_node_do_not_collide(adapter: SolarWindsAdapter) -> None:
    """Без имени правила в ключе второй алерт затёр бы первый."""
    other: dict[str, Any] = {
        **NESTED_PAYLOAD,
        "alert": {**NESTED_PAYLOAD["alert"], "name": "High Memory Utilization"},
    }

    assert adapter.parse(NESTED_PAYLOAD).external_key != adapter.parse(other).external_key


def test_flat_stand_payload_is_supported(adapter: SolarWindsAdapter) -> None:
    """Формат стенда: параметры в URL, вложенных объектов нет."""
    event = adapter.parse(load_fixture("solarwinds_trigger.json"))

    assert event.severity == 4
    assert event.object.name == "WINDOWS-STD3-80"
    assert event.object.ip == "83.166.251.15"
    assert event.rule.name == "High CPU Utilization"
    # Идентификатора узла стенд не присылает — ключом становится имя узла
    assert event.external_key == "WINDOWS-STD3-80|High CPU Utilization"
    assert event.occurred_at.astimezone(UTC) == datetime(2026, 8, 10, 9, 42, 10, tzinfo=UTC)
    # Значения метрик, не легшие в канонические поля, сохраняются
    assert event.attrs["cpu"] == "94"
    assert event.attrs["mem"] == "71"


def test_node_id_wins_over_node_name_when_present(adapter: SolarWindsAdapter) -> None:
    payload: dict[str, Any] = load_fixture("solarwinds_trigger.json")
    payload["nodeid"] = "1042"

    event = adapter.parse(payload)
    assert event.external_key == "1042|High CPU Utilization"
    assert event.object.external_id == "1042"


def test_value_is_extracted_from_message(adapter: SolarWindsAdapter) -> None:
    assert adapter.parse(NESTED_PAYLOAD).value == "94"


def test_value_stays_none_when_message_has_no_number(adapter: SolarWindsAdapter) -> None:
    """Неудачное извлечение — не ошибка: числа отдельным полем у источника нет."""
    payload: dict[str, Any] = {
        **NESTED_PAYLOAD,
        "alert": {**NESTED_PAYLOAD["alert"], "message": "Node is down"},
    }

    assert adapter.parse(payload).value is None


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("trigger", "problem"),
        ("Triggered", "problem"),
        ("reset", "resolved"),
        ("Reset", "resolved"),
        ("resolved", "resolved"),
    ],
)
def test_kind_mapping(adapter: SolarWindsAdapter, event_type: str, expected: str) -> None:
    """Гипотеза по reset-событию: открытый вопрос 1 в docs/tz.md."""
    payload: dict[str, Any] = {**NESTED_PAYLOAD, "eventType": event_type}

    assert adapter.parse(payload).kind == expected


def test_test_event_flag(adapter: SolarWindsAdapter) -> None:
    payload: dict[str, Any] = {**NESTED_PAYLOAD, "meta": {"testEvent": True}}

    assert adapter.parse(payload).is_test is True


def test_missing_alert_name_raises(adapter: SolarWindsAdapter) -> None:
    payload: dict[str, Any] = {**NESTED_PAYLOAD, "alert": {"objectId": "1042"}}

    with pytest.raises(AdapterError):
        adapter.parse(payload)


def test_unparseable_time_raises(adapter: SolarWindsAdapter) -> None:
    payload: dict[str, Any] = {**NESTED_PAYLOAD, "timestamp": "вчера вечером"}

    with pytest.raises(AdapterError):
        adapter.parse(payload)
