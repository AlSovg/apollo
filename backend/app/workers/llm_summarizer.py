"""Процесс сводок инцидентов.

    python -m app.workers.llm_summarizer

Отдельный процесс намеренно: модель отвечает секунды, а путь «событие →
инбокс» обязан укладываться в пять. Остановка этого воркера не влияет ни на
приём, ни на нормализацию, ни на доставку.
"""

import asyncio
import logging

from app.adapters.llm import build_llm_client
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.logging_conf import setup_logging
from app.services.llm import summarize_batch
from app.workers.runner import run_loop

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    sessionmaker = get_sessionmaker()
    client = build_llm_client(settings)

    async def tick() -> int:
        async with sessionmaker() as session, session.begin():
            result = await summarize_batch(
                session,
                client=client,
                prompt_version=settings.llm_prompt_version,
                limit=settings.llm_batch_size,
                mask_pii=settings.llm_mask_pii,
            )
        if result.size:
            logger.info(
                "summary batch",
                extra={"created": result.created, "failed": result.failed},
            )
        return result.created

    logger.info(
        "llm summarizer started",
        extra={"model": settings.llm_model, "prompt_version": settings.llm_prompt_version},
    )
    try:
        await run_loop(
            tick,
            batch_size=settings.llm_batch_size,
            poll_interval_s=settings.llm_poll_interval_s,
            name="llm_summarizer",
        )
    finally:
        await get_engine().dispose()
        logger.info("llm summarizer stopped")


if __name__ == "__main__":
    asyncio.run(main())
