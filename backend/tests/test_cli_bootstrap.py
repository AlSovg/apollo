"""Команды первичной настройки: вход в свежую систему и роли из каталога.

Без них развёртывание с нуля упирается в экран входа: локальных учёток нет,
а доменный пользователь без role_mappings приходит без ролей и в админку
не попадает.
"""

import io

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import verify_password
from app.cli import _create_admin, _list_role_mappings, _map_role, _unmap_role
from app.models.identity import RoleMapping, User, UserRole
from app.seed import DEMO_ROLE_MAPPINGS, DEMO_TOPICS, _seed


@pytest.fixture(autouse=True)
def _use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI работает через get_sessionmaker; в тестах подменяем на сессию БД теста."""

    class _Passthrough:
        """Не закрывает чужую сессию: транзакцией распоряжается тест."""

        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def __aenter__(self) -> AsyncSession:
            return self._session

        async def __aexit__(self, *exc: object) -> None:
            return None

    def _maker() -> object:
        # get_sessionmaker() возвращает фабрику, которую вызывают ещё раз
        return lambda: _Passthrough(db_session)

    monkeypatch.setattr("app.cli.get_sessionmaker", _maker)
    monkeypatch.setattr("app.seed.get_sessionmaker", _maker)
    monkeypatch.setattr(db_session, "commit", db_session.flush)
    # Команды печатают по-русски, а stdout под Windows остаётся в ascii —
    # проверяем логику, а не консоль
    monkeypatch.setattr("sys.stdout", io.StringIO())


async def test_create_admin_makes_working_login(db_session: AsyncSession) -> None:
    assert await _create_admin("breakglass", "Пароль-стенда-123") == 0

    user = (
        await db_session.execute(select(User).where(User.username == "breakglass"))
    ).scalar_one()
    assert user.provider == "local"
    assert user.is_active
    assert user.password_hash is not None
    assert verify_password(user.password_hash, "Пароль-стенда-123")

    roles = (
        await db_session.execute(select(UserRole.role_code).where(UserRole.user_id == user.id))
    ).scalars().all()
    assert "admin" in roles


async def test_create_admin_resets_password_of_existing(db_session: AsyncSession) -> None:
    """Команда восстанавливает доступ, а не только заводит учётку впервые."""
    await _create_admin("breakglass", "старый-пароль-123")
    user_id = (
        await db_session.execute(select(User.id).where(User.username == "breakglass"))
    ).scalar_one()

    assert await _create_admin("breakglass", "новый-пароль-456") == 0

    user = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    assert user.password_hash is not None
    assert verify_password(user.password_hash, "новый-пароль-456")
    assert not verify_password(user.password_hash, "старый-пароль-123")


async def test_map_role_rejects_unknown_role(db_session: AsyncSession) -> None:
    """Опечатка в коде роли не должна создавать мёртвое сопоставление."""
    assert await _map_role("CN=admins,DC=x", "суперадмин") == 1

    rows = (await db_session.execute(select(RoleMapping))).scalars().all()
    assert rows == []


async def test_map_and_unmap_role(db_session: AsyncSession) -> None:
    dn = "CN=platform-admins,OU=apollo-groups,DC=apollo,DC=test"

    assert await _map_role(dn, "admin") == 0
    assert await _map_role(dn, "admin") == 0  # повтор не ломается и не двоит

    rows = (await db_session.execute(select(RoleMapping))).scalars().all()
    assert [(row.external_group, row.role_code) for row in rows] == [(dn, "admin")]

    assert await _unmap_role(dn) == 0
    assert (await db_session.execute(select(RoleMapping))).scalars().all() == []
    assert await _list_role_mappings() == 0  # пустой список не падает


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    """Повторный прогон демо-данных не должен дублировать темы и маппинги."""
    from app.models.subscriptions import Topic

    await _seed(db_session)
    await _seed(db_session)

    topics = (await db_session.execute(select(Topic.code))).scalars().all()
    assert sorted(topics) == sorted(spec["code"] for spec in DEMO_TOPICS)

    mappings = (await db_session.execute(select(RoleMapping))).scalars().all()
    assert len(mappings) == len(DEMO_ROLE_MAPPINGS)
