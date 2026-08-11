"""Сводки инцидентов: сбор контекста, запись результата, устойчивость к отказу модели."""

from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.channels import WebChannel
from app.adapters.contacts import LocalContactResolver
from app.adapters.llm import IncidentContext, LlmResult, LlmUnavailableError
from app.models.delivery import LlmSummary, Notification
from app.services.delivery import deliver_batch
from app.services.llm import (
    build_context,
    load_prompt,
    render_context,
    summarize_batch,
    summarize_incident,
)
from app.services.router import route_batch
from tests.factories import PASSWORD, make_incident, make_topic, make_user, subscribe

PROMPT_VERSION = "v1"


class FakeClient:
    """Подстановка модели: тесты проверяют конвейер, а не качество текста."""

    def __init__(self, content: str = "Диск почти заполнен. Проверьте ротацию логов.") -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    async def summarize(self, prompt: str, context: str) -> LlmResult:
        self.calls.append((prompt, context))
        return LlmResult(content=self.content, model="qwen2.5-7b-instruct", latency_ms=120)


class DeadClient:
    async def summarize(self, prompt: str, context: str) -> LlmResult:
        raise LlmUnavailableError("модель недоступна после 2 попыток: ConnectError")


async def test_summary_is_stored_with_model_and_version(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    client = FakeClient()

    result = await summarize_incident(
        db_session, incident, client=client, prompt_version=PROMPT_VERSION
    )

    assert result.ok is True
    summary = (await db_session.execute(select(LlmSummary))).scalar_one()
    assert summary.incident_id == incident.id
    assert summary.model == "qwen2.5-7b-instruct"
    assert summary.prompt_version == PROMPT_VERSION
    assert summary.latency_ms == 120
    assert summary.masked is False


async def test_context_contains_incident_facts(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session, tags=["disk", "prod"])
    client = FakeClient()

    await summarize_incident(
        db_session, incident, client=client, prompt_version=PROMPT_VERSION
    )

    prompt, context = client.calls[0]
    assert "дежурного" in prompt  # промпт с версией подгрузился из файла
    assert "Диск заполнен на 94%" in context
    assert "db-prod-03" in context
    assert "disk, prod" in context
    assert "Критичность: 4 из 5" in context


async def test_dead_model_leaves_incident_without_summary(
    db_session: AsyncSession,
) -> None:
    incident = await make_incident(db_session)

    result = await summarize_incident(
        db_session, incident, client=DeadClient(), prompt_version=PROMPT_VERSION
    )

    assert result.ok is False
    assert result.error is not None
    assert await db_session.scalar(select(func.count()).select_from(LlmSummary)) == 0


async def test_dead_model_does_not_break_delivery(db_session: AsyncSession) -> None:
    """Сводка — обогащение: без неё уведомление всё равно уходит (FR-AI-4)."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    incident = await make_incident(db_session)

    await summarize_incident(
        db_session, incident, client=DeadClient(), prompt_version=PROMPT_VERSION
    )
    await route_batch(db_session)
    result = await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
    )

    assert result.sent == 1
    notification = (await db_session.execute(select(Notification))).scalar_one()
    assert notification.status == "sent"


async def test_incident_is_not_summarised_twice(db_session: AsyncSession) -> None:
    await make_incident(db_session)
    client = FakeClient()

    first = await summarize_batch(
        db_session, client=client, prompt_version=PROMPT_VERSION
    )
    second = await summarize_batch(
        db_session, client=client, prompt_version=PROMPT_VERSION
    )

    assert first.created == 1
    assert second.size == 0
    assert len(client.calls) == 1


async def test_new_prompt_version_creates_new_summary(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Смена версии промпта — повод пересчитать сводки и сравнить результаты."""
    incident = await make_incident(db_session)
    client = FakeClient()

    await summarize_incident(
        db_session, incident, client=client, prompt_version=PROMPT_VERSION
    )

    # Экспериментальная версия промпта живёт отдельным файлом
    (tmp_path / "incident_summary.v1-experiment.md").write_text(
        "Экспериментальный промпт.", encoding="utf-8"
    )
    monkeypatch.setattr("app.services.llm.PROMPTS_DIR", tmp_path)
    load_prompt.cache_clear()

    await summarize_incident(
        db_session, incident, client=client, prompt_version="v1-experiment"
    )
    load_prompt.cache_clear()

    versions = (
        await db_session.execute(select(LlmSummary.prompt_version))
    ).scalars().all()
    assert set(versions) == {PROMPT_VERSION, "v1-experiment"}


async def test_test_incidents_are_not_summarised(db_session: AsyncSession) -> None:
    await make_incident(db_session, is_test=True)
    client = FakeClient()

    result = await summarize_batch(db_session, client=client, prompt_version=PROMPT_VERSION)

    assert result.size == 0
    assert client.calls == []


async def test_batch_stops_when_model_is_down(db_session: AsyncSession) -> None:
    """Модель лежит — незачем долбить её всей пачкой."""
    for _ in range(3):
        await make_incident(db_session)

    result = await summarize_batch(
        db_session, client=DeadClient(), prompt_version=PROMPT_VERSION
    )

    assert result.size == 1
    assert result.created == 0


async def test_masking_hides_host_and_restores_it(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    client = FakeClient(content="На <HOST_1> закончилось место, проверьте логи.")

    result = await summarize_incident(
        db_session,
        incident,
        client=client,
        prompt_version=PROMPT_VERSION,
        mask_pii=True,
    )

    assert result.masked is True
    _, context = client.calls[0]
    assert "db-prod-03" not in context  # модель имени хоста не увидела

    summary = (await db_session.execute(select(LlmSummary))).scalar_one()
    assert summary.masked is True
    # А дежурный видит нормальный текст
    assert summary.content == "На db-prod-03 закончилось место, проверьте логи."


async def test_prompt_file_matches_requirements() -> None:
    prompt = load_prompt(PROMPT_VERSION)

    assert "2–4 предложения" in prompt
    assert "что случилось" in prompt
    assert "ТОЛЬКО факты" in prompt


async def test_missing_prompt_version_fails_loudly() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("несуществующая-версия")


async def test_render_context_omits_empty_fields() -> None:
    context = IncidentContext(
        incident_id=1,
        source="zabbix",
        severity=4,
        severity_raw="High",
        status="open",
        rule_name="Диск заполнен",
    )

    rendered = render_context(context)

    assert "Объект:" not in rendered
    assert "Теги:" not in rendered
    assert "Правило: Диск заполнен" in rendered


async def test_context_includes_event_feed(db_session: AsyncSession) -> None:
    from app.models.events import IncidentEvent

    incident = await make_incident(db_session)
    db_session.add(
        IncidentEvent(
            incident_id=incident.id,
            raw_event_id=1,
            kind="problem",
            event_ts=incident.opened_at,
            received_at=incident.opened_at,
            severity=4,
            message="Диск заполнен на 94%",
            value="94",
        )
    )
    await db_session.flush()

    context = await build_context(db_session, incident)

    assert len(context.events) == 1
    assert context.events[0].kind == "problem"
    assert "Лента событий:" in render_context(context)


async def test_summary_appears_in_inbox(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Сводка доезжает до дежурного через вьюху инбокса."""
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session, match_rule={})
    await subscribe(db_session, user, topic)
    incident = await make_incident(db_session)
    await summarize_incident(
        db_session, incident, client=FakeClient(), prompt_version=PROMPT_VERSION
    )
    await route_batch(db_session)
    await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
    )
    await db_session.commit()

    await db_client.post(
        "/api/v1/auth/login", json={"username": "duty", "password": PASSWORD}
    )
    item = (await db_client.get("/api/v1/notifications")).json()["items"][0]

    assert item["summary"] == "Диск почти заполнен. Проверьте ротацию логов."
