"""Административные экраны: пользователи и журнал аудита."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import Session as SessionRow
from app.models.identity import User, UserRole
from tests.factories import PASSWORD, make_user


async def login(client: httpx.AsyncClient, username: str) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200


async def test_user_list_shows_roles_and_no_personal_data(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    await make_user(db_session, "duty", role="engineer")
    await db_session.commit()

    await login(db_client, "admin")
    body = (await db_client.get("/api/v1/admin/users")).json()

    assert body["total"] == 2
    duty = next(item for item in body["items"] if item["username"] == "duty")
    assert duty["roles"] == [{"code": "engineer", "source": "local"}]
    # ПДн в системе не хранятся — показывать нечего
    assert "email" not in duty
    assert "full_name" not in duty


async def test_user_list_filters(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    await make_user(db_session, "duty", role="engineer")
    await make_user(db_session, "fired", is_active=False)
    await db_session.commit()

    await login(db_client, "admin")

    assert (await db_client.get("/api/v1/admin/users?query=dut")).json()["total"] == 1
    assert (await db_client.get("/api/v1/admin/users?is_active=false")).json()["total"] == 1
    assert (await db_client.get("/api/v1/admin/users?provider=ldap")).json()["total"] == 0


async def test_admin_sets_local_roles(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    duty = await make_user(db_session, "duty", role="engineer")
    await db_session.commit()

    await login(db_client, "admin")
    response = await db_client.put(
        f"/api/v1/admin/users/{duty.id}/roles", json={"roles": ["engineer", "viewer"]}
    )

    assert response.status_code == 200
    assert {role["code"] for role in response.json()["roles"]} == {"engineer", "viewer"}


async def test_directory_roles_are_not_touched_by_manual_assignment(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Роль из каталога перезапишет синхронизация — руками её не трогаем."""
    await make_user(db_session, "admin", role="admin")
    duty = await make_user(db_session, "duty")
    db_session.add(UserRole(user_id=duty.id, role_code="engineer", source="ldap"))
    await db_session.commit()

    await login(db_client, "admin")
    response = await db_client.put(
        f"/api/v1/admin/users/{duty.id}/roles", json={"roles": ["viewer"]}
    )

    roles = {role["code"]: role["source"] for role in response.json()["roles"]}
    assert roles == {"engineer": "ldap", "viewer": "local"}


async def test_unknown_role_is_rejected(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    duty = await make_user(db_session, "duty")
    await db_session.commit()

    await login(db_client, "admin")
    response = await db_client.put(
        f"/api/v1/admin/users/{duty.id}/roles", json={"roles": ["повелитель"]}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNKNOWN_ROLE"


async def test_deactivation_revokes_sessions(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    duty = await make_user(db_session, "duty", role="engineer")
    await db_session.commit()

    # У пользователя есть активная сессия
    await login(db_client, "duty")
    await login(db_client, "admin")

    response = await db_client.post(f"/api/v1/admin/users/{duty.id}/deactivate")

    assert response.status_code == 200
    assert response.json()["sessions_revoked"] >= 1

    await db_session.refresh(duty)
    assert duty.is_active is False
    sessions = (
        await db_session.execute(select(SessionRow).where(SessionRow.user_id == duty.id))
    ).scalars().all()
    assert all(row.revoked_at is not None for row in sessions)


async def test_admin_cannot_deactivate_himself(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Иначе администратор запирает сам себя."""
    admin = await make_user(db_session, "admin", role="admin")
    await db_session.commit()

    await login(db_client, "admin")
    response = await db_client.post(f"/api/v1/admin/users/{admin.id}/deactivate")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELF_DEACTIVATION"


async def test_admin_cannot_drop_his_own_admin_role(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Тот же самозапор, что и с отключением учётки, только через роли."""
    admin = await make_user(db_session, "admin", role="admin")
    await db_session.commit()

    await login(db_client, "admin")
    response = await db_client.put(
        f"/api/v1/admin/users/{admin.id}/roles", json={"roles": ["engineer"]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELF_ADMIN_LOSS"


async def test_admin_can_change_his_other_roles(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Запрет узкий: пока роль администратора остаётся, править остальные можно."""
    admin = await make_user(db_session, "admin", role="admin")
    await db_session.commit()

    await login(db_client, "admin")
    response = await db_client.put(
        f"/api/v1/admin/users/{admin.id}/roles", json={"roles": ["admin", "viewer"]}
    )

    assert response.status_code == 200
    assert {role["code"] for role in response.json()["roles"]} == {"admin", "viewer"}


async def test_audit_log_shows_actor_and_filters(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_user(db_session, "admin", role="admin")
    duty = await make_user(db_session, "duty")
    await db_session.commit()

    await login(db_client, "admin")
    await db_client.put(f"/api/v1/admin/users/{duty.id}/roles", json={"roles": ["viewer"]})

    body = (await db_client.get("/api/v1/admin/audit")).json()
    assert body["total"] >= 2  # вход и изменение ролей

    entry = next(item for item in body["items"] if item["action"] == "user.roles.set")
    assert entry["actor_username"] == "admin"
    assert entry["old_value"] == {"roles": []}
    assert entry["new_value"] == {"roles": ["viewer"]}

    filtered = (
        await db_client.get(f"/api/v1/admin/audit?action=user.roles.set&actor_id={admin.id}")
    ).json()
    assert filtered["total"] == 1


async def test_engineer_cannot_manage_users_or_read_audit(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    duty = await make_user(db_session, "duty", role="engineer")
    await db_session.commit()

    await login(db_client, "duty")

    assert (await db_client.get("/api/v1/admin/users")).status_code == 403
    assert (await db_client.get("/api/v1/admin/audit")).status_code == 403
    assert (
        await db_client.post(f"/api/v1/admin/users/{duty.id}/deactivate")
    ).status_code == 403


async def test_unknown_user_returns_404(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    await db_session.commit()

    await login(db_client, "admin")

    assert (
        await db_client.put("/api/v1/admin/users/999999/roles", json={"roles": []})
    ).status_code == 404
    assert (
        await db_client.post("/api/v1/admin/users/999999/deactivate")
    ).status_code == 404


async def test_deactivated_user_keeps_subscriptions(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """При восстановлении учётки подписки должны вернуться (FR-IDM-4)."""
    from sqlalchemy import func

    from app.models.subscriptions import Subscription
    from tests.factories import make_topic, subscribe

    await make_user(db_session, "admin", role="admin")
    duty = await make_user(db_session, "duty", role="engineer")
    topic = await make_topic(db_session)
    await subscribe(db_session, duty, topic)
    await db_session.commit()

    await login(db_client, "admin")
    await db_client.post(f"/api/v1/admin/users/{duty.id}/deactivate")

    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 1


async def test_users_are_paginated(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    for index in range(5):
        await make_user(db_session, f"duty{index}")
    await db_session.commit()

    await login(db_client, "admin")
    page = (await db_client.get("/api/v1/admin/users?page=2&size=2")).json()

    assert page["total"] == 6
    assert len(page["items"]) == 2


async def test_ldap_user_roles_are_visible(
    db_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "admin", role="admin")
    domain_user = User(subject_id="guid-1", provider="ldap", username="noc.duty")
    db_session.add(domain_user)
    await db_session.flush()
    db_session.add(UserRole(user_id=domain_user.id, role_code="engineer", source="ldap"))
    await db_session.commit()

    await login(db_client, "admin")
    body = (await db_client.get("/api/v1/admin/users?provider=ldap")).json()

    assert body["total"] == 1
    assert body["items"][0]["roles"] == [{"code": "engineer", "source": "ldap"}]
