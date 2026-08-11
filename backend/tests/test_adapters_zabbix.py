"""Адаптер Zabbix: проверяется каждое поле канонического события."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.adapters.sources import AdapterError, SeverityMap, UnknownSeverityError, ZabbixAdapter
from app.adapters.sources.base import parse_timezone
from tests.conftest import load_fixture

MOSCOW = parse_timezone("Europe/Moscow")

SEVERITY_MAP = SeverityMap(
    {
        ("zabbix", "0"): 0,
        ("zabbix", "1"): 1,
        ("zabbix", "2"): 2,
        ("zabbix", "3"): 3,
        ("zabbix", "4"): 4,
        ("zabbix", "5"): 5,
    }
)


@pytest.fixture
def adapter() -> ZabbixAdapter:
    return ZabbixAdapter(SEVERITY_MAP, MOSCOW)


def test_problem_fixture_maps_every_field(adapter: ZabbixAdapter) -> None:
    event = adapter.parse(load_fixture("zabbix_problem.json"))

    assert event.source_system == "zabbix"
    assert event.source_event_id == "59"
    assert event.external_key == "59"
    assert event.kind == "problem"
    assert event.severity == 4  # из event_nseverity, не из строки 'High'
    assert event.severity_raw == "High"

    assert event.object.external_id == "webhook-test-host"
    assert event.object.name == "webhook-test-host"
    assert event.object.ip is None  # '*UNKNOWN*' -> None
    assert event.object.object_group is None  # нерезолвнутый макрос -> None

    assert event.rule.key == "25239"
    assert event.rule.name == "Test webhook trigger"
    assert event.rule.expression == "last(/webhook-test-host/test.trap)=1"

    assert event.message is not None and event.message.startswith("Problem:")
    assert event.value == "1"
    assert event.tags == []
    assert event.is_test is False


def test_resolved_fixture_is_recognised(adapter: ZabbixAdapter) -> None:
    event = adapter.parse(load_fixture("zabbix_resolved.json"))

    assert event.kind == "resolved"
    # Ключ склейки тот же — это то же самое событие, что и PROBLEM
    assert event.external_key == "59"
    assert event.value == "0"


def test_timestamp_uses_configured_timezone(adapter: ZabbixAdapter) -> None:
    """Захардкоженный UTC сместил бы все инциденты на N часов."""
    event = adapter.parse(load_fixture("zabbix_problem.json"))

    assert event.occurred_at == datetime(
        2026, 8, 10, 10, 53, 52, tzinfo=timezone(timedelta(hours=3))
    )
    assert event.occurred_at.astimezone(UTC) == datetime(2026, 8, 10, 7, 53, 52, tzinfo=UTC)


def test_other_timezone_shifts_event_time() -> None:
    utc_adapter = ZabbixAdapter(SEVERITY_MAP, parse_timezone("UTC"))
    event = utc_adapter.parse(load_fixture("zabbix_problem.json"))

    assert event.occurred_at.astimezone(UTC) == datetime(2026, 8, 10, 10, 53, 52, tzinfo=UTC)


def test_fixed_offset_timezone_is_supported() -> None:
    adapter = ZabbixAdapter(SEVERITY_MAP, parse_timezone("+05:00"))
    event = adapter.parse(load_fixture("zabbix_problem.json"))

    assert event.occurred_at.utcoffset() == timedelta(hours=5)


def test_date_and_time_fields_are_used_without_timestamp(adapter: ZabbixAdapter) -> None:
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    del payload["timestamp"]

    event = adapter.parse(payload)
    assert event.occurred_at.hour == 10
    assert event.occurred_at.minute == 53


def test_unmapped_fields_go_to_attrs(adapter: ZabbixAdapter) -> None:
    """Ничего не выбрасываем: item_key, opdata и прочее остаются в attrs."""
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    payload["новое_поле_из_будущего"] = "значение"

    event = adapter.parse(payload)

    assert event.attrs["item_key"] == "test.trap"
    assert event.attrs["item_name"] == "Test trapper item"
    assert event.attrs["opdata"] == "1"
    assert event.attrs["новое_поле_из_будущего"] == "значение"
    # Поля, легшие в канонические колонки, в attrs не дублируются
    assert "event_id" not in event.attrs
    assert "trigger_name" not in event.attrs


@pytest.mark.parametrize(
    ("raw_tags", "expected"),
    [
        ([], []),
        (["disk", "prod"], ["disk", "prod"]),
        ([{"tag": "scope", "value": "availability"}], ["scope:availability"]),
        ([{"tag": "prod", "value": ""}], ["prod"]),
        ("disk, prod", ["disk", "prod"]),
        (None, []),
    ],
)
def test_tags_parsing(adapter: ZabbixAdapter, raw_tags: Any, expected: list[str]) -> None:
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    payload["event_tags"] = raw_tags

    assert adapter.parse(payload).tags == expected


def test_test_host_group_marks_event_as_test(adapter: ZabbixAdapter) -> None:
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    payload["hostgroup"] = "Test webhook group"

    assert adapter.parse(payload).is_test is True


@pytest.mark.parametrize(
    ("status", "event_value", "expected"),
    [
        ("PROBLEM", "1", "problem"),
        ("RESOLVED", "0", "resolved"),
        ("OK", "0", "resolved"),
        ("", "0", "resolved"),  # статуса нет — решает event_value
        ("", "1", "problem"),
    ],
)
def test_kind_mapping(
    adapter: ZabbixAdapter, status: str, event_value: str, expected: str
) -> None:
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    payload["status"] = status
    payload["event_value"] = event_value

    assert adapter.parse(payload).kind == expected


def test_unknown_severity_is_an_error(adapter: ZabbixAdapter) -> None:
    """Пропуск в severity_map — конфигурационная дыра, а не повод угадывать."""
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    payload["event_nseverity"] = "9"

    with pytest.raises(UnknownSeverityError):
        adapter.parse(payload)


@pytest.mark.parametrize("missing", ["event_id", "host_technical_name", "timestamp"])
def test_missing_required_field_raises(adapter: ZabbixAdapter, missing: str) -> None:
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    payload.pop(missing)
    if missing == "host_technical_name":
        payload.pop("host")
    if missing == "timestamp":
        payload.pop("date")
        payload.pop("time")

    with pytest.raises(AdapterError):
        adapter.parse(payload)


def test_parse_is_deterministic(adapter: ZabbixAdapter) -> None:
    """Повторный прогон одного события обязан давать тот же результат."""
    payload = load_fixture("zabbix_problem.json")
    assert adapter.parse(payload) == adapter.parse(payload)
