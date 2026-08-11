"""Периодическая синхронизация с каталогом (FR-IDM-3).

Отдельного контейнера задача не заслуживает: раз в час, идемпотентна, при
нескольких экземплярах API просто отработает несколько раз без вреда.
"""

import asyncio
import logging

from app.config import Settings
from app.db import get_sessionmaker
from app.services.login import sync_directory

logger = logging.getLogger(__name__)


async def directory_sync_loop(settings: Settings) -> None:
    interval = max(60, settings.ldap_sync_interval_minutes * 60)
    sessionmaker = get_sessionmaker()

    while True:
        await asyncio.sleep(interval)
        try:
            async with sessionmaker() as session:
                stats = await sync_directory(session, settings)
                await session.commit()
            logger.info("directory sync", extra=dict(stats))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Недоступность каталога не должна ронять приложение
            logger.warning("directory sync failed", exc_info=True)
