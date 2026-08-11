"""Точка входа API: приложение, роутеры, middleware."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.api.errors import register_error_handlers
from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.health import router as health_router
from app.api.v1.incidents import router as incidents_router
from app.api.v1.ingest import router as ingest_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.sources import router as sources_router
from app.api.v1.subscriptions import router as subscriptions_router
from app.api.v1.topics import router as topics_router
from app.api.v1.users import router as users_router
from app.config import get_settings
from app.logging_conf import setup_logging
from app.services.directory_sync import directory_sync_loop
from app.services.partitions import ensure_partitions

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("app.access")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Старт: досоздать месячные партиции — без них приём события упадёт.

    Недоступность БД на старте не мешает процессу подняться: /health отвечает,
    /health/ready честно вернёт 503, партиции досоздадутся при следующем старте.
    """
    settings = get_settings()
    try:
        await ensure_partitions(settings.partitions_months_ahead)
    except Exception:
        logger.warning("startup: не удалось создать партиции", exc_info=True)

    # Периодическая синхронизация ролей с каталогом (FR-IDM-3). Отдельный
    # контейнер под задачу раз в час заводить незачем — она идемпотентна.
    sync_task: asyncio.Task[None] | None = None
    if settings.auth_provider == "ldap":
        sync_task = asyncio.create_task(directory_sync_loop(settings))

    try:
        yield
    finally:
        if sync_task is not None:
            sync_task.cancel()


async def access_log_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Access-лог: request_id, метод, путь, статус, длительность. Тел — нет."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    access_logger.info(
        "request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(
        title="Alert Management Platform",
        version="0.1.0",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.middleware("http")(access_log_middleware)
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(ingest_router)
    app.include_router(auth_router)
    app.include_router(incidents_router)
    app.include_router(topics_router)
    app.include_router(subscriptions_router)
    app.include_router(notifications_router)
    app.include_router(sources_router)
    app.include_router(dashboard_router)
    app.include_router(users_router)
    app.include_router(admin_router)
    return app


app = create_app()
