from app.realtime.rate_limit import KeyedRateLimiter, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_burst_then_blocks():
    bucket = TokenBucket(rate=1, burst=3, clock=FakeClock())

    assert [bucket.allow() for _ in range(4)] == [True, True, True, False]


def test_refills_with_time():
    clock = FakeClock()
    bucket = TokenBucket(rate=2, burst=3, clock=clock)
    for _ in range(3):
        bucket.allow()

    clock.now = 0.4
    assert bucket.allow() is False
    clock.now = 0.5
    assert bucket.allow() is True
    assert bucket.allow() is False


def test_refill_capped_by_burst():
    clock = FakeClock()
    bucket = TokenBucket(rate=10, burst=2, clock=clock)

    clock.now = 100
    assert [bucket.allow() for _ in range(3)] == [True, True, False]


def test_is_full():
    clock = FakeClock()
    bucket = TokenBucket(rate=1, burst=2, clock=clock)
    assert bucket.is_full()

    bucket.allow()
    assert not bucket.is_full()

    clock.now = 1
    assert bucket.is_full()


# --- KeyedRateLimiter ---


def test_keys_are_limited_separately():
    limiter = KeyedRateLimiter(rate=1, burst=1, clock=FakeClock())

    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")


def test_new_keys_do_not_reset_exhausted_ones():
    # При переполнении забываем только полные вёдра: поток новых адресов не должен
    # обнулять лимит у того, кто его уже исчерпал
    limiter = KeyedRateLimiter(rate=0.001, burst=1, clock=FakeClock(), max_keys=2)
    assert limiter.allow("attacker")

    for i in range(10):
        limiter.allow(f"new-{i}")

    assert not limiter.allow("attacker")


def test_full_buckets_are_forgotten():
    clock = FakeClock()
    limiter = KeyedRateLimiter(rate=1, burst=1, clock=clock, max_keys=2)
    limiter.allow("a")
    limiter.allow("b")

    clock.now = 10
    limiter.allow("c")

    assert set(limiter._buckets) == {"c"}
