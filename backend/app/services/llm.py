"""Сводки инцидентов локальной моделью.

Сводка — обогащение, а не часть пути доставки: недоступность модели оставляет
инцидент без сводки, но не мешает уведомлению уйти (FR-AI-4).

Модель и версия промпта пишутся вместе с результатом, чтобы сводки были
воспроизводимы и сравнимы между прогонами (FR-AI-5).
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Select, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm import (
    IncidentContext,
    IncidentEventLine,
    LlmClient,
    LlmUnavailableError,
    Masker,
)
from app.models.delivery import LlmSummary
from app.models.events import Incident, IncidentEvent, MonitoredObject

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_NAME = "incident_summary"

# Сколько последних событий ленты показывать модели
MAX_EVENTS_IN_CONTEXT = 10


@dataclass
class SummaryResult:
    incident_id: int
    ok: bool
    masked: bool = False
    error: str | None = None


@dataclass
class SummaryBatchResult:
    results: list[SummaryResult] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.results)

    @property
    def created(self) -> int:
        return sum(1 for item in self.results if item.ok)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if not item.ok)


@lru_cache
def load_prompt(version: str) -> str:
    """Промпт лежит файлом рядом с кодом: его версия — часть результата."""
    path = PROMPTS_DIR / f"{PROMPT_NAME}.{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл промпта: {path}")
    return path.read_text(encoding="utf-8").strip()


def select_without_summary(limit: int, prompt_version: str) -> Select[tuple[Incident]]:
    """Инциденты без сводки текущей версии промпта.

    Сначала свежие: сводка нужна дежурному сейчас, а не в историческом архиве.
    Тестовые инциденты пропускаем — они и уведомлений не порождают.
    """
    has_summary = exists().where(
        LlmSummary.incident_id == Incident.id,
        LlmSummary.prompt_version == prompt_version,
    )
    return (
        select(Incident)
        .where(~has_summary, Incident.is_test.is_(False))
        .order_by(Incident.opened_at.desc())
        .limit(limit)
        .with_for_update(skip_locked=True, of=Incident)
    )


async def build_context(session: AsyncSession, incident: Incident) -> IncidentContext:
    """Контекст инцидента для модели."""
    obj = (
        await session.execute(
            select(MonitoredObject).where(MonitoredObject.id == incident.object_id)
        )
    ).scalar_one_or_none()

    events = (
        await session.execute(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident.id)
            .order_by(IncidentEvent.event_ts.desc())
            .limit(MAX_EVENTS_IN_CONTEXT)
        )
    ).scalars().all()

    return IncidentContext(
        incident_id=incident.id,
        source=incident.source,
        severity=incident.severity,
        severity_raw=incident.severity_raw,
        status=incident.status,
        rule_name=incident.rule_name,
        rule_expression=incident.rule_expr,
        object_name=obj.name if obj else None,
        object_ip=str(obj.ip) if obj and obj.ip else None,
        object_group=obj.object_group if obj else None,
        message=incident.last_message,
        value=None,
        tags=list(incident.tags or []),
        dedup_count=incident.dedup_count,
        opened_at=incident.opened_at,
        events=[
            IncidentEventLine(
                kind=event.kind,
                event_ts=event.event_ts,
                message=event.message,
                value=event.value,
            )
            for event in reversed(events)
        ],
    )


def render_context(context: IncidentContext) -> str:
    """Контекст в текст. Ничего, кроме этих строк, модель не увидит."""
    lines = [
        f"Источник: {context.source}",
        f"Правило: {context.rule_name}",
        f"Критичность: {context.severity} из 5 (в источнике: {context.severity_raw})",
        f"Статус: {'открыт' if context.status == 'open' else 'закрыт'}",
    ]
    if context.rule_expression:
        lines.append(f"Условие срабатывания: {context.rule_expression}")
    if context.object_name:
        lines.append(f"Объект: {context.object_name}")
    if context.object_ip:
        lines.append(f"Адрес объекта: {context.object_ip}")
    if context.object_group:
        lines.append(f"Группа объектов: {context.object_group}")
    if context.message:
        lines.append(f"Сообщение источника: {context.message}")
    if context.tags:
        lines.append(f"Теги: {', '.join(context.tags)}")
    if context.opened_at:
        lines.append(f"Начало: {context.opened_at:%Y-%m-%d %H:%M:%S %Z}".strip())
    if context.dedup_count > 1:
        lines.append(f"Повторов события: {context.dedup_count}")

    if context.events:
        lines.append("Лента событий:")
        for event in context.events:
            value = f", значение {event.value}" if event.value else ""
            lines.append(
                f"  {event.event_ts:%Y-%m-%d %H:%M:%S} — {event.kind}{value}"
            )
    return "\n".join(lines)


def build_masker(context: IncidentContext) -> Masker:
    """Значения из контекста, которые нельзя показывать модели при маскировании."""
    masker = Masker()
    masker.add(context.object_name, "HOST")
    masker.add(context.object_ip, "IP")
    masker.add(context.object_group, "GROUP")
    return masker


async def summarize_incident(
    session: AsyncSession,
    incident: Incident,
    *,
    client: LlmClient,
    prompt_version: str,
    mask_pii: bool = False,
) -> SummaryResult:
    context = await build_context(session, incident)
    rendered = render_context(context)

    masker = build_masker(context) if mask_pii else None
    if masker is not None:
        rendered = masker.mask(rendered)

    try:
        result = await client.summarize(load_prompt(prompt_version), rendered)
    except LlmUnavailableError as exc:
        # Инцидент остаётся без сводки и попадёт в следующий проход
        logger.warning(
            "llm summary skipped",
            extra={"incident_id": incident.id, "reason": str(exc)},
        )
        return SummaryResult(incident_id=incident.id, ok=False, error=str(exc))

    content = masker.unmask(result.content) if masker is not None else result.content
    session.add(
        LlmSummary(
            incident_id=incident.id,
            model=result.model,
            prompt_version=prompt_version,
            content=content,
            masked=bool(masker and masker.used),
            latency_ms=result.latency_ms,
        )
    )
    await session.flush()

    logger.info(
        "llm summary stored",
        extra={
            "incident_id": incident.id,
            "prompt_version": prompt_version,
            "latency_ms": result.latency_ms,
        },
    )
    return SummaryResult(
        incident_id=incident.id, ok=True, masked=bool(masker and masker.used)
    )


async def summarize_batch(
    session: AsyncSession,
    *,
    client: LlmClient,
    prompt_version: str,
    limit: int = 20,
    mask_pii: bool = False,
) -> SummaryBatchResult:
    result = SummaryBatchResult()
    incidents = (
        (await session.execute(select_without_summary(limit, prompt_version)))
        .scalars()
        .all()
    )

    for incident in incidents:
        async with session.begin_nested():
            outcome = await summarize_incident(
                session,
                incident,
                client=client,
                prompt_version=prompt_version,
                mask_pii=mask_pii,
            )
        result.results.append(outcome)
        if not outcome.ok:
            # Модель лежит — остальные инциденты в этой пачке тоже не пройдут
            break

    return result


def now() -> datetime:
    return datetime.now(tz=UTC)
