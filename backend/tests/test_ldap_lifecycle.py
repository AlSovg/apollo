"""Жизненный цикл учётных записей против настоящего каталога (FR-IDM).

Проверяется то, что заявлено критерием ИБ: вход по учётке каталога, роли из
групп, смена должности и увольнение. Каталог здесь настоящий — изменения
вносятся в него по-честному и откатываются после теста.
"""

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from ldap3 import MODIFY_ADD, MODIFY_DELETE, Connection, Server
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.identity import RoleMapping, User
from app.models.identity import Session as SessionRow
from app.models.subscriptions import Topic, TopicRole
from app.services.login import sync_directory
from tests.test_ldap_provider import ADMIN_DN, ADMIN_PASSWORD, BASE_DN, ldap_settings

ADMINS_GROUP = f"cn=platform-admins,ou=groups,{BASE_DN}"
ENGINEERS_GROUP = f"cn=noc-engineers,ou=groups,{BASE_DN}"
OWNERS_GROUP = f"cn=service-owners,ou=groups,{BASE_DN}"
ENGINEER_DN = f"uid=eng.network,ou=people,{BASE_DN}"


def ldap_connection() -> Connection:
    settings = ldap_settings()
    server = Server(settings.host, port=settings.port, use_ssl=False, connect_timeout=5)
    return Connection(server, user=ADMIN_DN, password=ADMIN_PASSWORD, auto_bind=True)


@pytest.fixture(scope="session")
def directory_available() -> None:
    try:
        ldap_connection().unbind()
    except Exception as exc:  # noqa: BLE001 — причина видна в пропуске
        pytest.skip(f"Каталог недоступен ({type(exc).__name__}: {exc})")


@pytest.fixture
def ldap_auth(directory_available: None) -> Iterator[None]:
    """Переключает приложение на каталог. Кода это не касается — только ENV."""
    settings = ldap_settings()
    previous = {key: os.environ.get(key) for key in _LDAP_ENV}
    os.environ.update(
        {
            "AUTH_PROVIDER": "ldap",
            "LDAP_HOST": settings.host,
            "LDAP_PORT": str(settings.port),
            "LDAP_USE_SSL": "false",
            "LDAP_BASE_DN": BASE_DN,
            "LDAP_BIND_DN": ADMIN_DN,
            "LDAP_BIND_PASSWORD": ADMIN_PASSWORD,
            "LDAP_USERNAME_ATTRIBUTE": "uid",
            "LDAP_ID_ATTRIBUTE": "entryUUID",
            "LDAP_GROUP_OBJECT_CLASS": "groupOfNames",
        }
    )
    get_settings.cache_clear()
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


_LDAP_ENV = (
    "AUTH_PROVIDER",
    "LDAP_HOST",
    "LDAP_PORT",
    "LDAP_USE_SSL",
    "LDAP_BASE_DN",
    "LDAP_BIND_DN",
    "LDAP_BIND_PASSWORD",
    "LDAP_USERNAME_ATTRIBUTE",
    "LDAP_ID_ATTRIBUTE",
    "LDAP_GROUP_OBJECT_CLASS",
)


@pytest.fixture
async def role_mappings(db_session: AsyncSession) -> AsyncIterator[None]:
    """Соответствие групп каталога внутренним ролям."""
    for group, role in (
        (ADMINS_GROUP, "admin"),
        (ENGINEERS_GROUP, "engineer"),
        (OWNERS_GROUP, "viewer"),
    ):
        db_session.add(RoleMapping(external_group=group, role_code=role))
    await db_session.commit()
    yield


async def login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


async def test_login_with_directory_account_maps_groups_to_roles(
    db_client: httpx.AsyncClient,
    db_session: AsyncSession,
    ldap_auth: None,
    role_mappings: None,
) -> None:
    response = await login(db_client, "noc.duty", "duty-pass")
    assert response.status_code == 200

    me = await db_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "noc.duty"
    assert body["roles"] == ["engineer"]
    # Имя пришло из каталога, но в БД его нет
    assert body["display_name"] == "Дежурный смены NOC"

    user = (
        await db_session.execute(select(User).where(User.username == "noc.duty"))
    ).scalar_one()
    assert user.provider == "ldap"
    assert user.password_hash is None  # пароль каталога не сохраняется


async def test_wrong_directory_password_is_rejected(
    db_client: httpx.AsyncClient, ldap_auth: None, role_mappings: None
) -> None:
    assert (await login(db_client, "noc.duty", "не тот")).status_code == 401


async def test_user_without_mapped_groups_gets_no_roles(
    db_client: httpx.AsyncClient, ldap_auth: None, role_mappings: None
) -> None:
    assert (await login(db_client, "no.groups", "nobody-pass")).status_code == 200

    assert (await db_client.get("/api/v1/auth/me")).json()["roles"] == []


async def test_role_change_in_directory_updates_mandatory_subscriptions(
    db_client: httpx.AsyncClient,
    db_session: AsyncSession,
    ldap_auth: None,
    role_mappings: None,
) -> None:
    """Смена должности: строки в subscriptions при этом не трогаются вообще."""
    await login(db_client, "eng.network", "eng-pass")
    user = (
        await db_session.execute(select(User).where(User.username == "eng.network"))
    ).scalar_one()

    # Тема, обязательная для инженеров
    topic = Topic(code="net-msk", name="Сеть МСК", match_rule={})
    db_session.add(topic)
    await db_session.flush()
    db_session.add(TopicRole(topic_id=topic.id, role_code="engineer"))
    await db_session.commit()

    async def effective_topics() -> list[int]:
        rows = await db_session.execute(
            text("SELECT topic_id FROM v_effective_subscriptions WHERE user_id = :uid"),
            {"uid": user.id},
        )
        return [row[0] for row in rows]

    assert await effective_topics() == [topic.id]

    # Перевод в другое подразделение: меняется только состав групп в каталоге
    conn = ldap_connection()
    conn.modify(ENGINEERS_GROUP, {"member": [(MODIFY_DELETE, [ENGINEER_DN])]})
    try:
        await sync_directory(db_session, get_settings())
        await db_session.commit()

        # Обязательная подписка перестала действовать сама
        assert await effective_topics() == []
        subscriptions = await db_session.scalar(text("SELECT count(*) FROM subscriptions"))
        assert subscriptions == 0  # материализованных строк не было и нет
    finally:
        conn.modify(ENGINEERS_GROUP, {"member": [(MODIFY_ADD, [ENGINEER_DN])]})
        conn.unbind()


async def test_removed_account_is_deactivated_and_sessions_revoked(
    db_client: httpx.AsyncClient,
    db_session: AsyncSession,
    ldap_auth: None,
    role_mappings: None,
) -> None:
    """Увольнение: учётки нет в каталоге → доступ закрыт, подписки сохранены."""
    temp_dn = f"uid=tmp.leaver,ou=people,{BASE_DN}"
    conn = ldap_connection()
    conn.add(
        temp_dn,
        "inetOrgPerson",
        {"cn": "Временный сотрудник", "sn": "Leaver", "userPassword": "temp-pass"},
    )
    conn.modify(ENGINEERS_GROUP, {"member": [(MODIFY_ADD, [temp_dn])]})
    conn.unbind()

    assert (await login(db_client, "tmp.leaver", "temp-pass")).status_code == 200
    user = (
        await db_session.execute(select(User).where(User.username == "tmp.leaver"))
    ).scalar_one()
    assert user.is_active is True

    # Учётку удалили из каталога
    conn = ldap_connection()
    conn.modify(ENGINEERS_GROUP, {"member": [(MODIFY_DELETE, [temp_dn])]})
    conn.delete(temp_dn)
    conn.unbind()

    stats = await sync_directory(db_session, get_settings())
    await db_session.commit()
    assert stats["deactivated"] >= 1

    await db_session.refresh(user)
    assert user.is_active is False

    sessions = (
        await db_session.execute(select(SessionRow).where(SessionRow.user_id == user.id))
    ).scalars().all()
    assert sessions and all(row.revoked_at is not None for row in sessions)

    # Доступ закрыт немедленно, не дожидаясь истечения токена
    assert (await db_client.get("/api/v1/auth/me")).status_code == 401


async def test_admin_can_trigger_sync(
    db_client: httpx.AsyncClient, ldap_auth: None, role_mappings: None
) -> None:
    await login(db_client, "admin.ops", "admin-pass")
    assert (await db_client.get("/api/v1/auth/me")).json()["roles"] == ["admin"]

    response = await db_client.post("/api/v1/admin/ldap/sync")

    assert response.status_code == 200
    assert response.json()["checked"] >= 1
