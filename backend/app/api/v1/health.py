"""Служебные эндпоинты живости и готовности.

/health — процесс жив. /health/ready — БД доступна: до готовности
балансировщик/оркестратор не должен слать трафик.
"""

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_READY_TIMEOUT_S = 5.0


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready() -> JSONResponse:
    try:
        async with asyncio.timeout(_READY_TIMEOUT_S):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        # Текст ошибки может содержать DSN с паролем — в лог только тип
        logger.warning("readiness: database unavailable", exc_info=True)
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(content={"status": "ready"})
