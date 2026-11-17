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
