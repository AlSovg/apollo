"""Процесс маршрутизации: инцидент → уведомления.

    python -m app.workers.router
"""

import asyncio
import logging

from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.logging_conf import setup_logging
from app.services.router import route_batch
from app.workers.runner import run_loop

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    sessionmaker = get_sessionmaker()

    async def tick() -> int:
        async with sessionmaker() as session, session.begin():
            result = await route_batch(session, limit=settings.router_batch_size)
        if result.size:
            logger.info(
                "routing batch",
                extra={
                    "incidents": result.size,
                    "notifications": result.created,
                    "skipped": result.skipped,
                },
            )
        return result.size

    logger.info("router started")
    try:
        await run_loop(
            tick,
            batch_size=settings.router_batch_size,
            poll_interval_s=settings.router_poll_interval_s,
            name="router",
        )
    finally:
        await get_engine().dispose()
        logger.info("router stopped")


if __name__ == "__main__":
    asyncio.run(main())
