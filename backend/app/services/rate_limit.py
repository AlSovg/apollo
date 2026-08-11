"""Ограничение частоты приёма — токенное ведро в памяти процесса.

Redis в контуре нет осознанно (раздел 2 AGENTS.md), поэтому счётчик локальный:
при N процессах приёма фактический предел — N × лимита. Для защиты от шторма
одного источника этого достаточно, а точный распределённый лимит потребовал бы
ещё одного компонента в периметре.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class RateLimiter:
    """Ведро на ключ: rate токенов в секунду, burst — ёмкость."""

    rate: float
    burst: float | None = None
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate должен быть положительным")
        self.capacity = self.burst if self.burst is not None else self.rate

    def allow(self, key: str) -> bool:
        now = self.clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(tokens=self.capacity - 1, updated_at=now)
            return True

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate)
        bucket.updated_at = now

        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True
