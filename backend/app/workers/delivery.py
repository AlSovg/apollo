"""Процесс доставки уведомлений.

    python -m app.workers.delivery

Экземпляров можно запускать несколько: очередь разбирается через
FOR UPDATE SKIP LOCKED, дублей не будет.
"""

import asyncio
import logging

from app.adapters.channels import build_channels
from app.adapters.contacts import build_contact_resolver
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.logging_conf import setup_logging
from app.services.delivery import deliver_batch
from app.workers.runner import run_loop

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    sessionmaker = get_sessionmaker()
    channels = build_channels(settings)

    async def tick() -> int:
        async with sessionmaker() as session, session.begin():
            result = await deliver_batch(
                session,
                channels=channels,
                contacts=build_contact_resolver(settings, session),
                limit=settings.delivery_batch_size,
                delays=settings.retry_delays,
            )
        if result.size:
            logger.info(
                "delivery batch",
                extra={
                    "sent": result.sent,
                    "failed": result.failed,
                    "retried": result.retried,
                },
            )
        return result.size

    logger.info("delivery started")
    try:
        await run_loop(
            tick,
            batch_size=settings.delivery_batch_size,
            poll_interval_s=settings.delivery_poll_interval_s,
            name="delivery",
        )
    finally:
        await get_engine().dispose()
        logger.info("delivery stopped")


if __name__ == "__main__":
    asyncio.run(main())
