"""Общие фикстуры тестов.

Тесты без БД работают всегда. Тестам с БД нужен поднятый PostgreSQL с
опубликованным портом:

    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

Реквизиты берутся из TEST_DATABASE_URL, иначе собираются из корневого `.env`.
Если БД недоступна, такие тесты помечаются как пропущенные — молча зелёными
они не становятся.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Адрес заведомо недоступен: тест готовности проверяет поведение без БД,
# а тесты с БД ходят по отдельному соединению (см. db_session)
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://apollo:apollo@127.0.0.1:1/apollo"
)
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-32-bytes")
# Тесты ходят по http, поэтому Secure-флаг мешал бы cookie доехать
os.environ.setdefault("COOKIE_SECURE", "false")

from app.api.v1.ingest import get_limiter  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.services.ingest import token_hash  # noqa: E402

TEST_DB_NAME = "apollo_test"


def _dotenv_values() -> dict[str, str]:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    env = _dotenv_values()
    user = env.get("POSTGRES_USER", "apollo")
    password = env.get("POSTGRES_PASSWORD", "apollo")
    port = os.environ.get("TEST_DB_PORT", "55432")
    return f"postgresql+asyncpg://{user}:{password}@127.0.0.1:{port}/{TEST_DB_NAME}"


async def _create_test_database(url: str) -> None:
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            from sqlalchemy import text

            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        await engine.dispose()


def _run_migrations(url: str) -> None:
    from alembic.config import Config

    from alembic import command

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def database_url() -> str:
    """Готовит тестовую БД. Пропускает тесты, если PostgreSQL недоступен."""
    url = test_database_url()
    try:
        asyncio.run(_create_test_database(url))
        _run_migrations(url)
    except Exception as exc:  # noqa: BLE001 — причина показывается в пропуске
        pytest.skip(f"Тестовая БД недоступна ({type(exc).__name__}: {exc})")
    return url


@pytest.fixture(scope="session")
async def db_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Один движок на прогон: создавать его на каждый тест — лишние сотни
    подключений и заметная доля времени всего набора."""
    engine = create_async_engine(database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Сессия внутри внешней транзакции: после теста всё откатывается.

    Коммиты внутри кода работают как вложенные точки сохранения, поэтому
    now() остаётся временем внешней транзакции — на этом строится проверка
    конфликта по (source, payload_hash, received_at).
    """
    connection = await db_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> Iterator[None]:
    """Ведро — глобальное, между тестами его нужно обнулять."""
    get_limiter.cache_clear()
    yield
    get_limiter.cache_clear()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Клиент без БД: используется тестами health."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def db_client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент, ходящий в тестовую БД через сессию с откатом."""

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def zabbix_token(db_session: AsyncSession) -> str:
    """Выдаёт источнику zabbix токен — как это делает `python -m app.cli issue-token`."""
    from sqlalchemy import update

    from app.models.events import EventSource

    token = "test-webhook-token"
    await db_session.execute(
        update(EventSource)
        .where(EventSource.code == "zabbix")
        .values(token_hash=token_hash(token), hmac_secret=None)
    )
    await db_session.commit()
    return token


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
