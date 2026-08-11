"""Правило отбора темы: таблица комбинаций.

Условия соединяются логическим И, внутри списка — ИЛИ. Это то место, куда
упрётся разбор «почему уведомление ушло не тому дежурному», поэтому проверяется
каждая комбинация отдельно.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from app.services.topics import IncidentFacts, MatchRule, match_topic

DISK_ALERT = IncidentFacts(
    source="zabbix",
    severity=4,
    object_group="prod-db",
    tags=["disk", "prod", "msk-dc1"],
)


@pytest.mark.parametrize(
    ("rule", "expected", "why"),
    [
        ({}, True, "пустое правило ловит все события"),
        # источник
        ({"sources": ["zabbix"]}, True, "источник совпал"),
        ({"sources": ["solarwinds"]}, False, "другой источник"),
        ({"sources": ["zabbix", "solarwinds"]}, True, "внутри списка — ИЛИ"),
        # критичность
        ({"severity_min": 4}, True, "ровно порог"),
        ({"severity_min": 3}, True, "выше порога"),
        ({"severity_min": 5}, False, "ниже порога"),
        ({"severity_min": 0}, True, "порог не ограничивает"),
        # группы объектов
        ({"object_groups": ["prod-db"]}, True, "группа совпала"),
        ({"object_groups": ["prod-web"]}, False, "другая группа"),
        ({"object_groups": ["prod-web", "prod-db"]}, True, "любая из групп"),
        # теги
        ({"tags_any": ["disk"]}, True, "один тег из списка"),
        ({"tags_any": ["cpu", "memory"]}, False, "ни одного совпадения"),
        ({"tags_any": ["cpu", "disk"]}, True, "достаточно одного"),
        ({"tags_all": ["disk", "prod"]}, True, "все теги на месте"),
        ({"tags_all": ["disk", "cpu"]}, False, "не хватает одного тега"),
        # комбинации: условия через И
        ({"sources": ["zabbix"], "severity_min": 4}, True, "оба условия выполнены"),
        ({"sources": ["zabbix"], "severity_min": 5}, False, "второе условие не выполнено"),
        ({"sources": ["solarwinds"], "severity_min": 0}, False, "первое условие не выполнено"),
        (
            {
                "sources": ["zabbix"],
                "severity_min": 3,
                "object_groups": ["prod-db"],
                "tags_any": ["disk"],
                "tags_all": ["prod"],
            },
            True,
            "все пять условий выполнены",
        ),
        (
            {
                "sources": ["zabbix"],
                "severity_min": 3,
                "object_groups": ["prod-db"],
                "tags_any": ["disk"],
                "tags_all": ["prod", "kzn"],
            },
            False,
            "одно условие из пяти не выполнено — тема не подходит",
        ),
    ],
)
def test_match_topic_table(rule: dict[str, Any], expected: bool, why: str) -> None:
    assert match_topic(DISK_ALERT, rule) is expected, why


def test_object_group_missing_on_incident() -> None:
    """У объекта нет группы: тема с фильтром по группе не должна срабатывать."""
    facts = IncidentFacts(source="zabbix", severity=4, object_group=None, tags=[])

    assert match_topic(facts, {"object_groups": ["prod-db"]}) is False
    assert match_topic(facts, {}) is True


def test_incident_without_tags() -> None:
    facts = IncidentFacts(source="zabbix", severity=2, tags=[])

    assert match_topic(facts, {"tags_any": ["disk"]}) is False
    assert match_topic(facts, {"tags_all": ["disk"]}) is False
    assert match_topic(facts, {"severity_min": 2}) is True


def test_unknown_field_in_rule_is_rejected() -> None:
    """Опечатка в правиле не должна превращать тему в «ловит всё»."""
    with pytest.raises(ValidationError):
        match_topic(DISK_ALERT, {"severity_minimum": 3})


def test_severity_outside_scale_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MatchRule.model_validate({"severity_min": 9})
