"""Процесс нормализации.

Запускается отдельным сервисом: приём вебхуков не должен тормозить из-за
обработки, а обработка — из-за приёма. Образ тот же, точка входа другая.

    python -m app.workers.normalizer
"""

import asyncio
import logging
from datetime import timedelta

from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.logging_conf import setup_logging
from app.services.normalizer import process_batch
from app.services.partitions import ensure_partitions
from app.workers.runner import run_loop

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    sessionmaker = get_sessionmaker()
    suppress_window = timedelta(minutes=settings.suppress_window_minutes)

    async def tick() -> int:
        # Транзакция держит блокировки до конца пачки: падение здесь означает
        # откат, а не потерю или двойную обработку событий
        async with sessionmaker() as session, session.begin():
            result = await process_batch(
                session,
                limit=settings.normalizer_batch_size,
                zabbix_tz=settings.zabbix_tz,
                suppress_window=suppress_window,
            )
        if result.size:
            logger.info(
                "batch processed",
                extra={"processed": result.processed, "failed": result.failed},
            )
        return result.size

    await ensure_partitions(settings.partitions_months_ahead)
    logger.info("normalizer started", extra={"batch": settings.normalizer_batch_size})
    try:
        await run_loop(
            tick,
            batch_size=settings.normalizer_batch_size,
            poll_interval_s=settings.normalizer_poll_interval_s,
            name="normalizer",
        )
    finally:
        await get_engine().dispose()
        logger.info("normalizer stopped")


if __name__ == "__main__":
    asyncio.run(main())
