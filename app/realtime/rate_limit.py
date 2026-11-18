import time
from collections.abc import Callable


class TokenBucket:
    """Разрешает burst событий подряд, дальше — rate событий в секунду."""

    def __init__(
        self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._tokens = float(burst)
        self._updated = clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def is_full(self) -> bool:
        return self._tokens + (self._clock() - self._updated) * self._rate >= self._burst


class KeyedRateLimiter:
    """Отдельный TokenBucket на каждый ключ, например на IP-адрес."""

    def __init__(
        self,
        rate: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, TokenBucket] = {}

    def allow(self, key: str) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                # Полное ведро ничем не отличается от нового — его можно забыть.
                # Исчерпанные остаются: иначе лимит сбрасывался бы потоком новых адресов
                self._buckets = {k: b for k, b in self._buckets.items() if not b.is_full()}
            bucket = self._buckets[key] = TokenBucket(self._rate, self._burst, self._clock)
        return bucket.allow()
