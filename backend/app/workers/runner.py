"""Общий цикл воркеров: опрос очереди, пауза, корректная остановка.

Отставание разбирается без пауз: если пачка заполнилась целиком, следующий
проход идёт сразу. На пустой очереди воркер спит и не греет БД.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop.set())


async def run_loop(
    tick: Callable[[], Awaitable[int]],
    *,
    batch_size: int,
    poll_interval_s: float,
    name: str,
    stop: asyncio.Event | None = None,
) -> None:
    stop_event = stop or asyncio.Event()
    if stop is None:
        install_signal_handlers(stop_event)

    while not stop_event.is_set():
        processed = 0
        try:
            processed = await tick()
        except Exception:
            # Недоступность БД или внешней системы не должна убивать процесс
            logger.exception("%s cycle failed", name)

        if processed >= batch_size:
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_s)
        except TimeoutError:
            continue
