from app.realtime.rate_limit import TokenBucket


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
