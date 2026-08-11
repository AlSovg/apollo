"""Тесты приёма вебхуков против реальной PostgreSQL."""

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.events import EventSource, RawEvent
from app.services.ingest import store_raw_event
from app.services.rate_limit import RateLimiter
from tests.conftest import load_fixture


async def _raw_events(session: AsyncSession) -> list[RawEvent]:
    result = await session.execute(select(RawEvent).order_by(RawEvent.id))
    return list(result.scalars())


async def test_real_zabbix_payload_is_accepted_and_stored(
    db_client: httpx.AsyncClient, db_session: AsyncSession, zabbix_token: str
) -> None:
    payload = load_fixture("zabbix_problem.json")
    body = json.dumps(payload, ensure_ascii=False).encode()

    response = await db_client.post(
        "/api/v1/ingest/zabbix",
        content=body,
        headers={"X-Webhook-Token": zabbix_token, "Content-Type": "application/json"},
    )

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "accepted"
    assert accepted["raw_event_id"] > 0

    stored = await _raw_events(db_session)
    assert len(stored) == 1
    assert stored[0].source == "zabbix"
    assert stored[0].payload == payload  # содержимое сохранено полностью
    assert stored[0].processed_at is None  # приём ничего не нормализует
    assert stored[0].payload_hash == hashlib.sha256(body).digest()


async def test_unknown_field_does_not_break_ingest(
    db_client: httpx.AsyncClient, db_session: AsyncSession, zabbix_token: str
) -> None:
    """Главное требование шага: неожиданное поле не должно терять событие."""
    payload: dict[str, Any] = load_fixture("zabbix_problem.json")
    payload["совершенно_новое_поле"] = {"nested": [1, 2, 3]}

    response = await db_client.post(
        "/api/v1/ingest/zabbix", json=payload, headers={"X-Webhook-Token": zabbix_token}
    )

    assert response.status_code == 202
    stored = await _raw_events(db_session)
    assert stored[0].payload["совершенно_новое_поле"] == {"nested": [1, 2, 3]}


async def test_solarwinds_query_parameters_are_accepted(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """SolarWinds шлёт параметры в URL, тело пустое."""
    from app.services.ingest import token_hash

    token = "sw-token"
    await db_session.execute(
        update(EventSource)
        .where(EventSource.code == "solarwinds")
        .values(token_hash=token_hash(token))
    )
    await db_session.commit()

    fixture = load_fixture("solarwinds_trigger.json")
    response = await db_client.post(
        "/api/v1/ingest/solarwinds",
        params=fixture,
        headers={"X-Webhook-Token": token},
    )

    assert response.status_code == 202
    stored = await _raw_events(db_session)
    assert stored[0].source == "solarwinds"
    assert stored[0].payload == fixture


async def test_wrong_token_is_rejected(
    db_client: httpx.AsyncClient, db_session: AsyncSession, zabbix_token: str
) -> None:
    response = await db_client.post(
        "/api/v1/ingest/zabbix",
        json=load_fixture("zabbix_problem.json"),
        headers={"X-Webhook-Token": "wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert await _raw_events(db_session) == []


async def test_missing_token_is_rejected(
    db_client: httpx.AsyncClient, zabbix_token: str
) -> None:
    response = await db_client.post(
        "/api/v1/ingest/zabbix", json=load_fixture("zabbix_problem.json")
    )
    assert response.status_code == 401


async def test_hmac_signature_is_verified(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from app.services.ingest import token_hash

    token, secret = "tok", b"webhook-secret"
    await db_session.execute(
        update(EventSource)
        .where(EventSource.code == "zabbix")
        .values(token_hash=token_hash(token), hmac_secret=secret)
    )
    await db_session.commit()

    body = json.dumps(load_fixture("zabbix_problem.json")).encode()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()

    bad = await db_client.post(
        "/api/v1/ingest/zabbix",
        content=body,
        headers={
            "X-Webhook-Token": token,
            "X-Webhook-Signature": "00" * 32,
            "Content-Type": "application/json",
        },
    )
    assert bad.status_code == 401

    good = await db_client.post(
        "/api/v1/ingest/zabbix",
        content=body,
        headers={
            "X-Webhook-Token": token,
            "X-Webhook-Signature": signature,
            "Content-Type": "application/json",
        },
    )
    assert good.status_code == 202


async def test_body_over_limit_is_rejected(
    db_client: httpx.AsyncClient, db_session: AsyncSession, zabbix_token: str
) -> None:
    from app.config import get_settings

    oversized = {"blob": "x" * (get_settings().ingest_max_body_bytes + 1024)}
    response = await db_client.post(
        "/api/v1/ingest/zabbix", json=oversized, headers={"X-Webhook-Token": zabbix_token}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert await _raw_events(db_session) == []


async def test_inactive_source_is_rejected(
    db_client: httpx.AsyncClient, db_session: AsyncSession, zabbix_token: str
) -> None:
    await db_session.execute(
        update(EventSource).where(EventSource.code == "zabbix").values(is_active=False)
    )
    await db_session.commit()

    response = await db_client.post(
        "/api/v1/ingest/zabbix",
        json=load_fixture("zabbix_problem.json"),
        headers={"X-Webhook-Token": zabbix_token},
    )
    assert response.status_code == 403

    # Отключение одного источника не влияет на остальные
    assert (await db_client.post("/api/v1/ingest/solarwinds", params={"a": "b"})).status_code == 401


async def test_unknown_source_returns_404(db_client: httpx.AsyncClient) -> None:
    response = await db_client.post("/api/v1/ingest/prometheus", json={"a": 1})
    assert response.status_code == 404


async def test_rate_limit_returns_429(
    db_client: httpx.AsyncClient, zabbix_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = RateLimiter(rate=1)
    monkeypatch.setattr("app.api.v1.ingest.get_limiter", lambda: limiter)
    payload = load_fixture("zabbix_problem.json")
    headers = {"X-Webhook-Token": zabbix_token}

    first = await db_client.post("/api/v1/ingest/zabbix", json=payload, headers=headers)
    second = await db_client.post("/api/v1/ingest/zabbix", json=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "1"


async def test_broken_json_returns_400(
    db_client: httpx.AsyncClient, zabbix_token: str
) -> None:
    response = await db_client.post(
        "/api/v1/ingest/zabbix",
        content="{сломанный json".encode(),
        headers={"X-Webhook-Token": zabbix_token, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_PAYLOAD"


async def test_repeated_delivery_is_not_an_error(
    db_client: httpx.AsyncClient, zabbix_token: str
) -> None:
    payload = load_fixture("zabbix_problem.json")
    headers = {"X-Webhook-Token": zabbix_token}

    first = await db_client.post("/api/v1/ingest/zabbix", json=payload, headers=headers)
    second = await db_client.post("/api/v1/ingest/zabbix", json=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202


async def test_exact_duplicate_returns_same_raw_event_id(db_session: AsyncSession) -> None:
    """Конфликт по (source, payload_hash, received_at).

    Внутри одной транзакции now() постоянна, поэтому повтор попадает ровно
    в ограничение raw_events_dedup_uq — тот случай, ради которого написана
    ветка ON CONFLICT.
    """
    payload = load_fixture("zabbix_problem.json")
    digest = hashlib.sha256(b"same-body").digest()

    first = await store_raw_event(db_session, source="zabbix", payload=payload, digest=digest)
    second = await store_raw_event(db_session, source="zabbix", payload=payload, digest=digest)

    assert first == second
    count = await db_session.scalar(select(func.count()).select_from(RawEvent))
    assert count == 1
