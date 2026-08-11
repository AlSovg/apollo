"""Темы и три происхождения подписок — через API и вьюху эффективных подписок."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import hash_password
from app.models.identity import User, UserRole
from app.models.subscriptions import Subscription, Topic, TopicRole

PASSWORD = "правильный-пароль-12"


async def _user(
    session: AsyncSession, username: str, role: str, subject: str
) -> User:
    user = User(
        subject_id=subject,
        provider="local",
        username=username,
        password_hash=hash_password(PASSWORD),
    )
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role_code=role, source="local"))
    await session.commit()
    return user


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await _user(db_session, "admin", "admin", "admin-uuid")


@pytest.fixture
async def engineer(db_session: AsyncSession) -> User:
    return await _user(db_session, "duty", "engineer", "engineer-uuid")


async def login(client: httpx.AsyncClient, username: str) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200


async def create_topic(
    client: httpx.AsyncClient, code: str = "prod-db", **overrides: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": code,
        "name": "БД prod",
        "match_rule": {"sources": ["zabbix"], "severity_min": 3},
    }
    payload.update(overrides)
    response = await client.post("/api/v1/topics", json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json())


# --- каталог тем ------------------------------------------------------------


async def test_only_admin_can_create_topic(
    db_client: httpx.AsyncClient, engineer: User
) -> None:
    await login(db_client, "duty")

    response = await db_client.post(
        "/api/v1/topics", json={"code": "any", "name": "Любая", "match_rule": {}}
    )

    assert response.status_code == 403


async def test_topic_rule_is_validated(db_client: httpx.AsyncClient, admin: User) -> None:
    await login(db_client, "admin")

    response = await db_client.post(
        "/api/v1/topics",
        json={"code": "broken", "name": "Тема", "match_rule": {"severity_minimum": 3}},
    )

    assert response.status_code == 422  # опечатка в правиле не проходит


async def test_topic_deletion_is_soft(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)

    assert (await db_client.delete(f"/api/v1/topics/{topic['id']}")).status_code == 204

    # Запись осталась — история подписок и уведомлений не должна разъезжаться
    stored = await db_session.get(Topic, int(str(topic["id"])))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.is_active is False
    assert [t["id"] for t in (await db_client.get("/api/v1/topics")).json()] == []


# --- обязательные подписки --------------------------------------------------


async def test_role_binding_creates_mandatory_subscription(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    """Приёмка шага: создать тему, привязать к роли, выдать роль."""
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    response = await db_client.put(
        f"/api/v1/topics/{topic['id']}/roles", json={"roles": ["engineer"]}
    )
    assert response.status_code == 200

    await login(db_client, "duty")
    subscriptions = (await db_client.get("/api/v1/subscriptions")).json()

    assert len(subscriptions["mandatory"]) == 1
    mandatory = subscriptions["mandatory"][0]
    assert mandatory["topic_id"] == topic["id"]
    assert mandatory["origin"] == "mandatory_role"
    assert mandatory["can_unsubscribe"] is False
    assert subscriptions["voluntary"] == []

    # Строк в subscriptions при этом не появилось
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 0


async def test_topic_list_marks_state_for_user(
    db_client: httpx.AsyncClient, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    mandatory = await create_topic(db_client, code="prod-db")
    await create_topic(db_client, code="net-msk")
    await db_client.put(f"/api/v1/topics/{mandatory['id']}/roles", json={"roles": ["engineer"]})

    await login(db_client, "duty")
    states = {t["code"]: t["state"] for t in (await db_client.get("/api/v1/topics")).json()}

    assert states == {"prod-db": "mandatory", "net-msk": "available"}


async def test_removing_role_removes_mandatory_subscription(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    """Смена должности не требует правок в subscriptions."""
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    await db_client.put(f"/api/v1/topics/{topic['id']}/roles", json={"roles": ["engineer"]})

    await login(db_client, "duty")
    assert len((await db_client.get("/api/v1/subscriptions")).json()["mandatory"]) == 1

    await db_session.execute(delete(UserRole).where(UserRole.user_id == engineer.id))
    await db_session.commit()

    subscriptions = (await db_client.get("/api/v1/subscriptions")).json()
    assert subscriptions["mandatory"] == []
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 0


async def test_mandatory_subscription_cannot_be_unsubscribed(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    await db_client.put(f"/api/v1/topics/{topic['id']}/roles", json={"roles": ["engineer"]})

    await login(db_client, "duty")
    mandatory = (await db_client.get("/api/v1/subscriptions")).json()["mandatory"][0]

    # Отписываться не от чего: строки подписки не существует
    assert mandatory["subscription_id"] is None
    assert (await db_client.delete("/api/v1/subscriptions/999999")).status_code == 404


# --- добровольные подписки --------------------------------------------------


async def test_self_subscribe_and_unsubscribe(
    db_client: httpx.AsyncClient, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)

    await login(db_client, "duty")
    created = await db_client.post("/api/v1/subscriptions", json={"topic_id": topic["id"]})
    assert created.status_code == 201
    body = created.json()
    assert body["origin"] == "self"
    assert body["can_unsubscribe"] is True

    subscriptions = (await db_client.get("/api/v1/subscriptions")).json()
    assert len(subscriptions["voluntary"]) == 1
    assert subscriptions["mandatory"] == []

    assert (
        await db_client.delete(f"/api/v1/subscriptions/{body['subscription_id']}")
    ).status_code == 204
    assert (await db_client.get("/api/v1/subscriptions")).json()["voluntary"] == []


async def test_double_self_subscription_is_rejected(
    db_client: httpx.AsyncClient, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)

    await login(db_client, "duty")
    await db_client.post("/api/v1/subscriptions", json={"topic_id": topic["id"]})
    repeated = await db_client.post("/api/v1/subscriptions", json={"topic_id": topic["id"]})

    assert repeated.status_code == 409


async def test_self_subscription_on_mandatory_topic_collapses_to_one(
    db_client: httpx.AsyncClient, admin: User, engineer: User
) -> None:
    """Приоритет происхождения: mandatory_role побеждает self."""
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    await db_client.put(f"/api/v1/topics/{topic['id']}/roles", json={"roles": ["engineer"]})

    await login(db_client, "duty")
    assert (
        await db_client.post("/api/v1/subscriptions", json={"topic_id": topic["id"]})
    ).status_code == 201

    subscriptions = (await db_client.get("/api/v1/subscriptions")).json()
    assert len(subscriptions["mandatory"]) == 1
    assert subscriptions["voluntary"] == []
    assert subscriptions["mandatory"][0]["origin"] == "mandatory_role"


async def test_unknown_channel_is_rejected(
    db_client: httpx.AsyncClient, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)

    await login(db_client, "duty")
    response = await db_client.post(
        "/api/v1/subscriptions", json={"topic_id": topic["id"], "channels": ["telegram"]}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNKNOWN_CHANNEL"


# --- глушение ---------------------------------------------------------------


async def test_mute_is_capped(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)

    await login(db_client, "duty")
    created = (
        await db_client.post("/api/v1/subscriptions", json={"topic_id": topic["id"]})
    ).json()

    forever = datetime.now(tz=UTC) + timedelta(days=365)
    response = await db_client.patch(
        f"/api/v1/subscriptions/{created['subscription_id']}",
        json={"muted_until": forever.isoformat()},
    )

    assert response.status_code == 200
    muted_until = datetime.fromisoformat(response.json()["muted_until"])
    assert muted_until < datetime.now(tz=UTC) + timedelta(hours=25)


async def test_muting_mandatory_topic_does_not_disable_delivery(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    """Обязательная подписка бьёт глушение (правило из раздела 4 AGENTS.md)."""
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    await db_client.put(f"/api/v1/topics/{topic['id']}/roles", json={"roles": ["engineer"]})

    await login(db_client, "duty")
    created = (
        await db_client.post("/api/v1/subscriptions", json={"topic_id": topic["id"]})
    ).json()
    await db_client.patch(
        f"/api/v1/subscriptions/{created['subscription_id']}",
        json={"muted_until": (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()},
    )

    mandatory = (await db_client.get("/api/v1/subscriptions")).json()["mandatory"][0]
    assert mandatory["origin"] == "mandatory_role"
    assert mandatory["muted_until"] is None  # глушение проигнорировано


# --- назначенные подписки ---------------------------------------------------


async def test_admin_assigns_subscription_user_cannot_remove_it(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    assigned = await db_client.post(
        "/api/v1/admin/subscriptions",
        json={"user_id": engineer.id, "topic_id": topic["id"]},
    )
    assert assigned.status_code == 201
    subscription_id = assigned.json()["subscription_id"]

    await login(db_client, "duty")
    listed = (await db_client.get("/api/v1/subscriptions")).json()
    assert len(listed["mandatory"]) == 1
    assert listed["mandatory"][0]["origin"] == "assigned"
    assert listed["mandatory"][0]["can_unsubscribe"] is False

    response = await db_client.delete(f"/api/v1/subscriptions/{subscription_id}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SUBSCRIPTION_MANDATORY"


async def test_admin_can_revoke_assigned_subscription(
    db_client: httpx.AsyncClient, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    assigned = (
        await db_client.post(
            "/api/v1/admin/subscriptions",
            json={"user_id": engineer.id, "topic_id": topic["id"]},
        )
    ).json()

    revoked = await db_client.delete(
        f"/api/v1/admin/subscriptions/{assigned['subscription_id']}"
    )
    assert revoked.status_code == 204

    await login(db_client, "duty")
    assert (await db_client.get("/api/v1/subscriptions")).json()["mandatory"] == []


async def test_engineer_cannot_assign_subscriptions(
    db_client: httpx.AsyncClient, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)

    await login(db_client, "duty")
    response = await db_client.post(
        "/api/v1/admin/subscriptions",
        json={"user_id": engineer.id, "topic_id": topic["id"]},
    )

    assert response.status_code == 403


async def test_subscription_of_another_user_is_not_visible(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    assigned = (
        await db_client.post(
            "/api/v1/admin/subscriptions",
            json={"user_id": engineer.id, "topic_id": topic["id"]},
        )
    ).json()

    # Админ не должен править чужую подписку через пользовательский эндпоинт
    response = await db_client.patch(
        f"/api/v1/subscriptions/{assigned['subscription_id']}", json={"channels": ["web"]}
    )
    assert response.status_code == 404


async def test_topic_roles_binding_is_audited(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    from app.models.system import AuditLog

    await login(db_client, "admin")
    topic = await create_topic(db_client)
    await db_client.put(f"/api/v1/topics/{topic['id']}/roles", json={"roles": ["engineer"]})

    actions = [
        row.action for row in (await db_session.execute(select(AuditLog))).scalars().all()
    ]
    assert "topic.create" in actions
    assert "topic.roles.set" in actions


async def test_topic_role_binding_creates_no_subscription_rows(
    db_client: httpx.AsyncClient, db_session: AsyncSession, admin: User, engineer: User
) -> None:
    """Ключевое свойство схемы: обязательные подписки не материализуются."""
    await login(db_client, "admin")
    topic = await create_topic(db_client)
    await db_client.put(f"/api/v1/topics/{topic['id']}/roles", json={"roles": ["engineer"]})

    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 0
    assert await db_session.scalar(select(func.count()).select_from(TopicRole)) == 1
