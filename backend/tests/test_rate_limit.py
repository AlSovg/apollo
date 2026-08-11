import pytest

from app.services.rate_limit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_burst_then_blocks() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate=3, clock=clock)

    assert [limiter.allow("zabbix") for _ in range(3)] == [True, True, True]
    assert limiter.allow("zabbix") is False


def test_refills_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate=2, clock=clock)

    assert limiter.allow("zabbix") is True
    assert limiter.allow("zabbix") is True
    assert limiter.allow("zabbix") is False

    clock.now += 1.0  # прошла секунда — набежало 2 токена
    assert limiter.allow("zabbix") is True
    assert limiter.allow("zabbix") is True
    assert limiter.allow("zabbix") is False


def test_sources_are_limited_independently() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate=1, clock=clock)

    assert limiter.allow("zabbix") is True
    assert limiter.allow("zabbix") is False
    # Шторм одного источника не должен глушить другой
    assert limiter.allow("solarwinds") is True


def test_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(rate=0)
