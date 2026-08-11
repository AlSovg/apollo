"""Реестр источников через админский API и приём событий от новых источников."""

import json

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.events import EventSource, Incident, RawEvent
from app.models.identity import User
from app.models.system import AuditLog
from app.models.system import SeverityMap as SeverityMapRow
from app.services.normalizer import process_batch
from tests.conftest import load_fixture
from tests.factories import PASSWORD, make_user
from tests.test_source_registry import ALERTMANAGER_MAP, CANONICAL_PAYLOAD


async def admin_login(client: httpx.AsyncClient, session: AsyncSession) -> User:
    user = await make_user(session, "admin", role="admin")
    await session.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 200
    return user


async def register_prometheus(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/v1/admin/sources",
        json={
            "code": "prometheus",
            "name": "Prometheus Alertmanager",
            "kind": "mapping",
            "field_map": ALERTMANAGER_MAP,
        },
    )


async def add_severity_levels(session: AsyncSession, source: str) -> None:
    for raw_value, severity in (("critical", 4), ("warning", 2), ("high", 4)):
        session.add(
            SeverityMapRow(
                source=source, raw_value=raw_value, severity=severity, label=raw_value
            )
        )
    await session.flush()


async def test_source_is_registered_and_token_issued(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await admin_login(db_client, db_session)

    created = await register_prometheus(db_client)
    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "mapping"
    assert body["token_set"] is False  # токен ещё не выдан

    issued = await db_client.post("/api/v1/admin/sources/prometheus/rotate-token")
    assert issued.status_code == 200
    assert len(issued.json()["token"]) > 20

    listed = (await db_client.get("/api/v1/admin/sources")).json()
    prometheus = next(item for item in listed if item["code"] == "prometheus")
    assert prometheus["token_set"] is True
    # Сам токен и его хеш наружу не отдаются
    assert "token" not in prometheus
    assert "token_hash" not in prometheus


async def test_test_endpoint_shows_resulting_event(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Кнопка «тест»: интегратор видит результат разбора, не сохраняя ничего."""
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await add_severity_levels(db_session, "prometheus")
    await db_session.commit()

    response = await db_client.post(
        "/api/v1/admin/sources/prometheus/test",
        json={"payload": load_fixture("prometheus_alertmanager.json")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["event"]["external_key"] == "a1b2c3d4e5f60718"
    assert body["event"]["rule"]["name"] == "HostOutOfMemory"
    assert body["event"]["severity"] == 4

    # Пробный разбор ничего не записал
    assert await db_session.scalar(select(RawEvent.id).limit(1)) is None


async def test_test_endpoint_explains_broken_mapping(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await db_session.commit()

    response = await db_client.post(
        "/api/v1/admin/sources/prometheus/test",
        json={
            "payload": load_fixture("prometheus_alertmanager.json"),
            "field_map": {**ALERTMANAGER_MAP, "message": "$.alerts[*].summary"},
        },
    )

    body = response.json()
    assert body["ok"] is False
    assert body["problems"]
    assert "не поддерживается" in " ".join(body["problems"])


async def test_invalid_field_map_is_rejected_on_save(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Опечатка в маппинге не должна доехать до боевого приёма."""
    await admin_login(db_client, db_session)

    response = await db_client.post(
        "/api/v1/admin/sources",
        json={
            "code": "broken",
            "name": "Битый",
            "kind": "mapping",
            "field_map": {"externall_key": "$.a"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "FIELD_MAP_INVALID"


async def test_prometheus_event_becomes_incident_without_new_code(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Приёмка шага: новый источник подключён целиком через админку."""
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await add_severity_levels(db_session, "prometheus")
    await db_session.commit()

    token = (
        await db_client.post("/api/v1/admin/sources/prometheus/rotate-token")
    ).json()["token"]

    accepted = await db_client.post(
        "/api/v1/ingest/prometheus",
        content=json.dumps(load_fixture("prometheus_alertmanager.json")).encode(),
        headers={"X-Webhook-Token": token, "Content-Type": "application/json"},
    )
    assert accepted.status_code == 202

    result = await process_batch(db_session)
    assert result.failed == 0

    incident = (
        await db_session.execute(select(Incident).where(Incident.source == "prometheus"))
    ).scalar_one()
    assert incident.external_key == "a1b2c3d4e5f60718"
    assert incident.rule_name == "HostOutOfMemory"
    assert incident.severity == 4


async def test_canonical_source_needs_no_mapping_at_all(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Самый дешёвый уровень подключения: готовый канонический JSON."""
    await admin_login(db_client, db_session)
    await add_severity_levels(db_session, "generic")
    await db_session.commit()

    token = (
        await db_client.post("/api/v1/admin/sources/generic/rotate-token")
    ).json()["token"]

    accepted = await db_client.post(
        "/api/v1/ingest/generic",
        content=json.dumps(CANONICAL_PAYLOAD).encode(),
        headers={"X-Webhook-Token": token, "Content-Type": "application/json"},
    )
    assert accepted.status_code == 202

    result = await process_batch(db_session)
    assert result.failed == 0

    incident = (
        await db_session.execute(select(Incident).where(Incident.source == "generic"))
    ).scalar_one()
    assert incident.external_key == "8841203"


async def test_broken_expression_lands_in_process_error(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Событие не теряется: после исправления маппинга его переобработают."""
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await add_severity_levels(db_session, "prometheus")
    await db_session.commit()

    token = (
        await db_client.post("/api/v1/admin/sources/prometheus/rotate-token")
    ).json()["token"]
    await db_client.post(
        "/api/v1/ingest/prometheus",
        content=json.dumps(load_fixture("prometheus_alertmanager.json")).encode(),
        headers={"X-Webhook-Token": token, "Content-Type": "application/json"},
    )

    # Маппинг ломается уже после приёма события
    await db_session.execute(
        update(EventSource)
        .where(EventSource.code == "prometheus")
        .values(field_map={**ALERTMANAGER_MAP, "occurred_at": "$.нет.поля"})
    )
    await db_session.commit()

    result = await process_batch(db_session)
    assert result.failed == 1

    raw = (
        await db_session.execute(select(RawEvent).where(RawEvent.source == "prometheus"))
    ).scalar_one()
    assert raw.process_error is not None
    assert raw.processed_at is None  # событие переобработается

    # Чиним маппинг — событие разбирается без повторной отправки
    await db_session.execute(
        update(EventSource)
        .where(EventSource.code == "prometheus")
        .values(field_map=ALERTMANAGER_MAP)
    )
    await db_session.commit()

    repeated = await process_batch(db_session)
    assert repeated.processed == 1


async def test_token_of_one_source_does_not_work_for_another(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await db_session.commit()

    prometheus_token = (
        await db_client.post("/api/v1/admin/sources/prometheus/rotate-token")
    ).json()["token"]

    response = await db_client.post(
        "/api/v1/ingest/zabbix",
        content=b"{}",
        headers={"X-Webhook-Token": prometheus_token, "Content-Type": "application/json"},
    )

    assert response.status_code == 401


async def test_rotation_does_not_affect_other_sources(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await db_session.commit()

    zabbix_token = (
        await db_client.post("/api/v1/admin/sources/zabbix/rotate-token")
    ).json()["token"]
    await db_client.post("/api/v1/admin/sources/prometheus/rotate-token")

    response = await db_client.post(
        "/api/v1/ingest/zabbix",
        content=json.dumps(load_fixture("zabbix_problem.json")).encode(),
        headers={"X-Webhook-Token": zabbix_token, "Content-Type": "application/json"},
    )

    assert response.status_code == 202  # токен Zabbix продолжает работать


async def test_deactivated_source_is_rejected_others_keep_working(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await db_session.commit()

    prometheus_token = (
        await db_client.post("/api/v1/admin/sources/prometheus/rotate-token")
    ).json()["token"]
    zabbix_token = (
        await db_client.post("/api/v1/admin/sources/zabbix/rotate-token")
    ).json()["token"]

    assert (await db_client.delete("/api/v1/admin/sources/prometheus")).status_code == 204

    disabled = await db_client.post(
        "/api/v1/ingest/prometheus",
        content=b"{}",
        headers={"X-Webhook-Token": prometheus_token, "Content-Type": "application/json"},
    )
    assert disabled.status_code == 403

    alive = await db_client.post(
        "/api/v1/ingest/zabbix",
        content=json.dumps(load_fixture("zabbix_problem.json")).encode(),
        headers={"X-Webhook-Token": zabbix_token, "Content-Type": "application/json"},
    )
    assert alive.status_code == 202


async def test_source_changes_are_audited(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)
    await db_client.post("/api/v1/admin/sources/prometheus/rotate-token")
    await db_client.delete("/api/v1/admin/sources/prometheus")

    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    actions = {row.action for row in rows}

    assert {"source.create", "source.token.rotate", "source.deactivate"} <= actions
    # Значение токена в журнал не попадает
    assert all("token" not in json.dumps(row.new_value or {}) for row in rows)


async def test_engineer_cannot_manage_sources(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "duty", role="engineer")
    await db_session.commit()
    await db_client.post(
        "/api/v1/auth/login", json={"username": "duty", "password": PASSWORD}
    )

    assert (await db_client.get("/api/v1/admin/sources")).status_code == 403
    assert (await register_prometheus(db_client)).status_code == 403


async def test_duplicate_source_code_is_rejected(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await admin_login(db_client, db_session)
    await register_prometheus(db_client)

    assert (await register_prometheus(db_client)).status_code == 409
