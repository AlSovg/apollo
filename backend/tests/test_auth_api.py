"""Вход, сессии, RBAC и жизненный цикл учёток — через API и БД."""

import json

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import hash_password
from app.config import get_settings
from app.models.identity import Session as SessionRow
from app.models.identity import User, UserRole
from app.models.system import AuditLog
from app.services.auth import ACCESS_COOKIE, REFRESH_COOKIE

LOCAL_PASSWORD = "правильный-пароль-12"


@pytest.fixture
async def local_admin(db_session: AsyncSession) -> User:
    user = User(
        subject_id="local-admin-uuid",
        provider="local",
        username="admin",
        password_hash=hash_password(LOCAL_PASSWORD),
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserRole(user_id=user.id, role_code="admin", source="local"))
    await db_session.commit()
    return user


@pytest.fixture
async def local_viewer(db_session: AsyncSession) -> User:
    user = User(
        subject_id="local-viewer-uuid",
        provider="local",
        username="viewer",
        password_hash=hash_password(LOCAL_PASSWORD),
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserRole(user_id=user.id, role_code="viewer", source="local"))
    await db_session.commit()
    return user


async def login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


async def test_login_sets_httponly_cookies_and_returns_no_tokens(
    db_client: httpx.AsyncClient, local_admin: User
) -> None:
    response = await login(db_client, "admin", LOCAL_PASSWORD)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # Токенов в теле нет — они живут только в cookie
    assert "token" not in response.text.lower()

    cookies = {name: value for name, value in response.cookies.items()}
    assert ACCESS_COOKIE in cookies
    assert REFRESH_COOKIE in cookies

    raw = response.headers.get_list("set-cookie")
    assert all("HttpOnly" in header for header in raw)
    assert all("SameSite=strict" in header.replace("samesite", "SameSite") for header in raw)


async def test_me_returns_roles(db_client: httpx.AsyncClient, local_admin: User) -> None:
    await login(db_client, "admin", LOCAL_PASSWORD)

    response = await db_client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "admin"
    assert body["subject_id"] == "local-admin-uuid"
    assert body["roles"] == ["admin"]


async def test_wrong_password_is_rejected_and_not_stored(
    db_client: httpx.AsyncClient, db_session: AsyncSession, local_admin: User
) -> None:
    response = await login(db_client, "admin", "неверный-пароль")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"

    # Пароль не попал ни в БД, ни в журнал аудита
    await db_session.refresh(local_admin)
    assert local_admin.password_hash is not None
    assert "неверный-пароль" not in local_admin.password_hash

    audit = (await db_session.execute(select(AuditLog))).scalars().all()
    assert any(row.action == "user.login.failed" for row in audit)
    assert all("неверный-пароль" not in json.dumps(row.new_value or {}) for row in audit)


async def test_account_is_locked_after_failed_attempts(
    db_client: httpx.AsyncClient, db_session: AsyncSession, local_admin: User
) -> None:
    settings = get_settings()
    for _ in range(settings.max_failed_logins):
        assert (await login(db_client, "admin", "мимо")).status_code == 401

    # Даже с верным паролем вход закрыт до истечения блокировки
    response = await login(db_client, "admin", LOCAL_PASSWORD)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCOUNT_LOCKED"

    await db_session.refresh(local_admin)
    assert local_admin.locked_until is not None


async def test_successful_login_resets_failed_counter(
    db_client: httpx.AsyncClient, db_session: AsyncSession, local_admin: User
) -> None:
    await login(db_client, "admin", "мимо")
    await login(db_client, "admin", LOCAL_PASSWORD)

    await db_session.refresh(local_admin)
    assert local_admin.failed_logins == 0
    assert local_admin.last_login_at is not None


async def test_refresh_rotates_token_and_revokes_the_old_one(
    db_client: httpx.AsyncClient, db_session: AsyncSession, local_admin: User
) -> None:
    await login(db_client, "admin", LOCAL_PASSWORD)
    first_refresh = db_client.cookies[REFRESH_COOKIE]

    response = await db_client.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    assert db_client.cookies[REFRESH_COOKIE] != first_refresh

    rows = (
        await db_session.execute(
            select(SessionRow).where(SessionRow.user_id == local_admin.id)
        )
    ).scalars().all()
    assert len(rows) == 2
    revoked = [row for row in rows if row.revoked_at is not None]
    assert len(revoked) == 1
    assert revoked[0].replaced_by is not None


async def test_reused_refresh_token_is_rejected(
    db_client: httpx.AsyncClient, local_admin: User
) -> None:
    """Повторное предъявление ротированного токена — признак кражи."""
    await login(db_client, "admin", LOCAL_PASSWORD)
    stolen = db_client.cookies[REFRESH_COOKIE]

    await db_client.post("/api/v1/auth/refresh")

    db_client.cookies.set(REFRESH_COOKIE, stolen, path="/api")
    response = await db_client.post("/api/v1/auth/refresh")
    assert response.status_code == 401


async def test_logout_revokes_session(
    db_client: httpx.AsyncClient, db_session: AsyncSession, local_admin: User
) -> None:
    await login(db_client, "admin", LOCAL_PASSWORD)

    assert (await db_client.post("/api/v1/auth/logout")).status_code == 200

    rows = (await db_session.execute(select(SessionRow))).scalars().all()
    assert all(row.revoked_at is not None for row in rows)
    assert (await db_client.post("/api/v1/auth/refresh")).status_code == 401


async def test_deactivated_user_loses_access_immediately(
    db_client: httpx.AsyncClient, db_session: AsyncSession, local_admin: User
) -> None:
    """Access-токен ещё жив, но учётка отключена — доступа быть не должно."""
    await login(db_client, "admin", LOCAL_PASSWORD)
    assert (await db_client.get("/api/v1/auth/me")).status_code == 200

    await db_session.execute(
        update(User).where(User.id == local_admin.id).values(is_active=False)
    )
    await db_session.commit()

    assert (await db_client.get("/api/v1/auth/me")).status_code == 401
    assert (await login(db_client, "admin", LOCAL_PASSWORD)).status_code == 401


async def test_anonymous_access_is_rejected(db_client: httpx.AsyncClient) -> None:
    assert (await db_client.get("/api/v1/auth/me")).status_code == 401


async def test_viewer_cannot_use_admin_endpoint(
    db_client: httpx.AsyncClient, local_viewer: User
) -> None:
    await login(db_client, "viewer", LOCAL_PASSWORD)

    response = await db_client.post("/api/v1/admin/ldap/sync")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_admin_sync_requires_ldap_provider(
    db_client: httpx.AsyncClient, local_admin: User
) -> None:
    """С локальным провайдером синхронизировать нечего — это не 500."""
    await login(db_client, "admin", LOCAL_PASSWORD)

    response = await db_client.post("/api/v1/admin/ldap/sync")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUTH_PROVIDER_NOT_LDAP"


async def test_login_is_recorded_in_audit_log(
    db_client: httpx.AsyncClient, db_session: AsyncSession, local_admin: User
) -> None:
    await login(db_client, "admin", LOCAL_PASSWORD)

    actions = [
        row.action for row in (await db_session.execute(select(AuditLog))).scalars().all()
    ]
    assert "user.login" in actions
