"""Подключение к PostgreSQL: async engine, фабрика сессий, зависимость FastAPI."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            # На пике приёма (100 событий/с) запросов больше, чем даёт пул
            # по умолчанию: пятнадцати соединений не хватает, и приём начинает
            # ждать не базу, а свободное соединение
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # БД на отдельном узле: при потере маршрута пакеты молча
            # отбрасываются, и без лимита asyncpg ждёт соединения 60 с
            connect_args={"timeout": settings.db_connect_timeout_s},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: одна сессия на запрос."""
    async with get_sessionmaker()() as session:
        yield session
