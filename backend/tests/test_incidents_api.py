"""API инцидентов: список для дашборда и карточка."""

from datetime import UTC, datetime

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm import LlmResult
from app.models.events import Incident, IncidentEvent
from app.services.llm import summarize_incident
from tests.factories import PASSWORD, make_incident, make_user


class FakeClient:
    async def summarize(self, prompt: str, context: str) -> LlmResult:
        return LlmResult(
            content="Диск на db-prod-03 почти заполнен. Проверьте ротацию логов.",
            model="qwen2.5-7b-instruct",
            latency_ms=95,
        )


async def login(client: httpx.AsyncClient, username: str = "duty") -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200


async def test_incident_card_contains_summary(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Приёмка шага 8: карточка инцидента отдаёт сводку модели."""
    await make_user(db_session, "duty", role="engineer")
    incident = await make_incident(db_session)
    await summarize_incident(
        db_session, incident, client=FakeClient(), prompt_version="v1"
    )
    await db_session.commit()

    await login(db_client)
    body = (await db_client.get(f"/api/v1/incidents/{incident.id}")).json()

    assert body["id"] == incident.id
    assert body["rule_name"] == "Диск заполнен на 94%"
    assert body["object_name"] == "db-prod-03"
    # Колонка inet приходит объектом IPv4Address — наружу должна уйти строкой
    assert body["object_ip"] == "10.0.3.14"
    assert body["summary"]["content"].startswith("Диск на db-prod-03")
    assert body["summary"]["model"] == "qwen2.5-7b-instruct"
    assert body["summary"]["prompt_version"] == "v1"
    assert body["summary"]["masked"] is False


async def test_incident_without_summary_is_still_readable(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Модель могла не успеть — карточка обязана открываться и без сводки."""
    await make_user(db_session, "duty", role="engineer")
    incident = await make_incident(db_session)
    await db_session.commit()

    await login(db_client)
    body = (await db_client.get(f"/api/v1/incidents/{incident.id}")).json()

    assert body["summary"] is None
    assert body["status"] == "open"


async def test_incident_card_contains_event_feed(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "duty", role="engineer")
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
    await db_session.commit()

    await login(db_client)
    card = (await db_client.get(f"/api/v1/incidents/{incident.id}")).json()
    events = (await db_client.get(f"/api/v1/incidents/{incident.id}/events")).json()

    assert len(card["events"]) == 1
    assert card["events"][0]["kind"] == "problem"
    assert card["events"][0]["value"] == "94"
    assert len(events) == 1


async def test_list_filters(db_client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session, severity=5, source="zabbix")
    await make_incident(db_session, severity=2, source="zabbix")
    solarwinds = await make_incident(db_session, severity=4, source="solarwinds")
    # incidents_resolved_chk: закрытый инцидент обязан знать время закрытия
    await db_session.execute(
        update(Incident)
        .where(Incident.id == solarwinds.id)
        .values(status="resolved", resolved_at=datetime.now(tz=UTC))
    )
    await db_session.commit()

    await login(db_client)

    assert (await db_client.get("/api/v1/incidents")).json()["total"] == 3
    assert (await db_client.get("/api/v1/incidents?severity_min=4")).json()["total"] == 2
    assert (await db_client.get("/api/v1/incidents?source=solarwinds")).json()["total"] == 1
    assert (await db_client.get("/api/v1/incidents?status=open")).json()["total"] == 2


async def test_list_is_sorted_by_severity_then_time(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session, severity=2)
    await make_incident(db_session, severity=5)
    await db_session.commit()

    await login(db_client)
    items = (await db_client.get("/api/v1/incidents")).json()["items"]

    assert [item["severity"] for item in items] == [5, 2]


async def test_test_incidents_are_hidden_by_default(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """FR-INC-7: тестовые инциденты не попадают в основной дашборд."""
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session, is_test=True)
    await db_session.commit()

    await login(db_client)

    assert (await db_client.get("/api/v1/incidents")).json()["total"] == 0
    assert (await db_client.get("/api/v1/incidents?include_test=true")).json()["total"] == 1


async def test_pagination(db_client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session, "duty", role="engineer")
    for _ in range(5):
        await make_incident(db_session)
    await db_session.commit()

    await login(db_client)
    page = (await db_client.get("/api/v1/incidents?page=2&size=2")).json()

    assert page["total"] == 5
    assert len(page["items"]) == 2
    assert page["page"] == 2


async def test_unknown_incident_returns_404(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "duty", role="engineer")
    await db_session.commit()

    await login(db_client)

    assert (await db_client.get("/api/v1/incidents/999999")).status_code == 404


async def test_ack_returns_501_while_feature_is_off(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Квитирование заложено структурно, но выключено (FR-FUT-1)."""
    await make_user(db_session, "duty", role="engineer")
    incident = await make_incident(db_session)
    await db_session.commit()

    await login(db_client)
    response = await db_client.post(f"/api/v1/incidents/{incident.id}/ack")

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "NOT_IMPLEMENTED"


async def test_incidents_require_authentication(db_client: httpx.AsyncClient) -> None:
    assert (await db_client.get("/api/v1/incidents")).status_code == 401


async def test_search_by_rule_and_object(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Дежурный ищет по тому, что видит: названию правила или имени объекта."""
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session)
    await db_session.commit()

    await login(db_client)

    assert (await db_client.get("/api/v1/incidents?search=Диск")).json()["total"] == 1
    assert (await db_client.get("/api/v1/incidents?search=db-prod")).json()["total"] == 1
    assert (await db_client.get("/api/v1/incidents?search=такого нет")).json()["total"] == 0


async def test_severity_counts_cover_whole_selection(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Счётчики считаются по всей выборке, а не по текущей странице."""
    await make_user(db_session, "duty", role="engineer")
    for _ in range(3):
        await make_incident(db_session, severity=5)
    await make_incident(db_session, severity=2)
    await db_session.commit()

    await login(db_client)
    body = (await db_client.get("/api/v1/incidents?page=1&size=1")).json()

    assert len(body["items"]) == 1  # на странице одна строка
    assert body["severity_counts"]["5"] == 3  # а счётчики знают про все
    assert body["severity_counts"]["2"] == 1
    assert body["severity_counts"]["0"] == 0


async def test_severity_counts_respect_filters(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session, severity=5, source="zabbix")
    await make_incident(db_session, severity=4, source="solarwinds")
    await db_session.commit()

    await login(db_client)
    body = (await db_client.get("/api/v1/incidents?source=zabbix")).json()

    assert body["severity_counts"]["5"] == 1
    assert body["severity_counts"]["4"] == 0


async def test_exact_severity_filter_matches_its_counter(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Плитка обещает число — список обязан показать ровно его.

    Раньше плитки фильтровали «этот уровень и выше», и «Средняя · 11»
    открывала 122 строки вместе с высокими и авариями.
    """
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session, severity=5)
    await make_incident(db_session, severity=3)
    await make_incident(db_session, severity=3)
    await db_session.commit()

    await login(db_client)
    body = (await db_client.get("/api/v1/incidents?severity=3")).json()

    assert body["total"] == body["severity_counts"]["3"] == 2
    assert {item["severity"] for item in body["items"]} == {3}


async def test_severity_counts_ignore_severity_filter(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Выбор уровня не должен обнулять остальные плитки.

    Счётчики — это навигация: дежурный, отобрав аварии, обязан видеть, сколько
    инцидентов ждёт его на других уровнях, иначе фильтр «слепнет» после клика.
    """
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session, severity=5)
    await make_incident(db_session, severity=2)
    await db_session.commit()

    await login(db_client)
    body = (await db_client.get("/api/v1/incidents?severity_min=5")).json()

    assert body["total"] == 1  # список отфильтрован
    assert body["severity_counts"]["5"] == 1
    assert body["severity_counts"]["2"] == 1  # а плитка нижнего уровня жива


async def test_severity_counts_still_respect_other_filters_with_severity(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Остальные условия на счётчики влияют даже при выбранном уровне."""
    await make_user(db_session, "duty", role="engineer")
    await make_incident(db_session, severity=5, source="zabbix")
    await make_incident(db_session, severity=2, source="solarwinds")
    await db_session.commit()

    await login(db_client)
    body = (await db_client.get("/api/v1/incidents?severity_min=5&source=zabbix")).json()

    assert body["severity_counts"]["5"] == 1
    assert body["severity_counts"]["2"] == 0
