"""Правило отбора событий темой.

Вся сложность фильтрации спрятана здесь, поэтому в интерфейсе подписок нет
ни фильтров по severity, ни по тегам: пользователь видит плоский список тем,
а условия настраивает администратор (раздел 4 AGENTS.md).

Условия соединяются логическим И, внутри списка — ИЛИ. Пустое правило ловит
все события.
"""

from dataclasses import dataclass, field

from pydantic import Field

from app.models.events import Incident
from app.schemas.base import ApiModel


class MatchRule(ApiModel):
    """Правило темы. Неизвестное поле — ошибка валидации, а не тихий пропуск."""

    sources: list[str] | None = None
    severity_min: int | None = Field(default=None, ge=0, le=5)
    object_groups: list[str] | None = None
    tags_any: list[str] | None = None
    tags_all: list[str] | None = None


@dataclass
class IncidentFacts:
    """Признаки инцидента, по которым отбирают темы.

    Группа объекта живёт в monitored_objects, поэтому её передают отдельно —
    матчер остаётся чистой функцией и тестируется без БД.
    """

    source: str
    severity: int
    object_group: str | None = None
    tags: list[str] = field(default_factory=list)


def incident_facts(incident: Incident, object_group: str | None = None) -> IncidentFacts:
    return IncidentFacts(
        source=incident.source,
        severity=incident.severity,
        object_group=object_group,
        tags=list(incident.tags or []),
    )


def match_rule(facts: IncidentFacts, rule: MatchRule) -> bool:
    if rule.sources and facts.source not in rule.sources:
        return False
    if rule.severity_min is not None and facts.severity < rule.severity_min:
        return False
    if rule.object_groups and facts.object_group not in rule.object_groups:
        return False

    tags = set(facts.tags)
    if rule.tags_any and not tags & set(rule.tags_any):
        return False
    if rule.tags_all and not set(rule.tags_all) <= tags:
        return False
    return True


def match_topic(facts: IncidentFacts, topic_rule: dict[str, object]) -> bool:
    """Подходит ли инцидент под правило темы.

    Правило хранится в jsonb, поэтому валидируется на входе: битое правило
    не должно молча ловить всё подряд.
    """
    return match_rule(facts, MatchRule.model_validate(topic_rule or {}))
