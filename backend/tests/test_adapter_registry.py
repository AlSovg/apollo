"""Реестр адаптеров и разбор всех фикстур разом."""

from datetime import UTC, timedelta
from typing import Any

import pytest

from app.adapters.sources import (
    ADAPTERS,
    SeverityMap,
    SolarWindsAdapter,
    UnknownAdapterError,
    ZabbixAdapter,
    build_adapter,
    resolve_timezone,
)
from app.adapters.sources.base import CanonicalEvent, parse_timezone
from tests.conftest import load_fixture

SEVERITY_MAP = SeverityMap(
    {
        ("zabbix", "4"): 4,
        ("solarwinds", "critical"): 4,
    }
)


def test_registry_resolves_by_adapter_class_name() -> None:
    zabbix = build_adapter("ZabbixAdapter", SEVERITY_MAP, UTC)
    solarwinds = build_adapter("SolarWindsAdapter", SEVERITY_MAP, UTC)

    assert isinstance(zabbix, ZabbixAdapter)
    assert isinstance(solarwinds, SolarWindsAdapter)
    assert zabbix.source == "zabbix"
    assert solarwinds.source == "solarwinds"


def test_unknown_adapter_class_raises() -> None:
    with pytest.raises(UnknownAdapterError):
        build_adapter("PrometheusAdapter", SEVERITY_MAP, UTC)

    with pytest.raises(UnknownAdapterError):
        build_adapter(None, SEVERITY_MAP, UTC)


def test_new_source_only_needs_a_registry_entry() -> None:
    """Третий источник не должен требовать правок в существующих адаптерах."""

    class PrometheusAdapter:
        source = "prometheus"

        def __init__(self, severity_map: SeverityMap, timezone: Any) -> None:
            self._severity_map = severity_map

        def parse(self, payload: dict[str, Any]) -> CanonicalEvent:  # pragma: no cover
            raise NotImplementedError

    ADAPTERS["PrometheusAdapter"] = PrometheusAdapter
    try:
        adapter = build_adapter("PrometheusAdapter", SEVERITY_MAP, UTC)
        assert adapter.source == "prometheus"
        # Существующие адаптеры продолжают работать
        assert build_adapter("ZabbixAdapter", SEVERITY_MAP, UTC).source == "zabbix"
    finally:
        del ADAPTERS["PrometheusAdapter"]


def test_resolve_timezone_prefers_source_registry_value() -> None:
    assert resolve_timezone("Europe/Moscow", "UTC") == parse_timezone("Europe/Moscow")
    # У источника не задана — берётся настройка приложения
    assert resolve_timezone(None, "Europe/Moscow") == parse_timezone("Europe/Moscow")
    # Не задано нигде — UTC, а не случайная зона хоста
    assert resolve_timezone(None, None) is UTC


def test_fixed_offset_and_utc_are_parsed() -> None:
    assert parse_timezone("+03:00").utcoffset(None) == timedelta(hours=3)
    assert parse_timezone("-0500").utcoffset(None) == timedelta(hours=-5)
    assert parse_timezone("UTC") is UTC


@pytest.mark.parametrize(
    ("fixture_name", "adapter_class", "source"),
    [
        ("zabbix_problem.json", "ZabbixAdapter", "zabbix"),
        ("zabbix_resolved.json", "ZabbixAdapter", "zabbix"),
        ("solarwinds_trigger.json", "SolarWindsAdapter", "solarwinds"),
    ],
)
def test_every_fixture_parses(fixture_name: str, adapter_class: str, source: str) -> None:
    severity_map = SeverityMap({("zabbix", "4"): 4, ("solarwinds", "critical"): 4})
    adapter = build_adapter(severity_map=severity_map, adapter_class=adapter_class, timezone=UTC)

    event = adapter.parse(load_fixture(fixture_name))

    assert event.source_system == source
    assert event.external_key
    assert event.rule.name
    assert event.object.external_id
    assert event.occurred_at.tzinfo is not None  # время всегда со смещением
