import time

import anyio
import pytest

from app.security.upload_limit import UploadRateLimiter

pytestmark = pytest.mark.anyio

IP, OTHER_IP = "203.0.113.7", "198.51.100.9"
# Пополнение почти нулевое: проверяем только запас подряд
NO_REFILL = 0.001


async def test_burst_then_refused(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=3)

    assert [await limiter.allow(IP) for _ in range(4)] == [True, True, True, False]


async def test_addresses_are_counted_separately(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=1)

    assert await limiter.allow(IP)
    assert not await limiter.allow(IP)
    assert await limiter.allow(OTHER_IP)


async def test_tokens_refill_over_time(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=10, burst=1)
    assert await limiter.allow(IP)
    assert not await limiter.allow(IP)

    # Токен копится 0,1 с по часам Redis. Ждём с запасом, а не ровно: системные часы
    # в Docker Desktop подводит синхронизация. Отказ токен не тратит — опрашивать можно
    deadline = time.monotonic() + 2
    while not await limiter.allow(IP):
        assert time.monotonic() < deadline, "token was not refilled"
        await anyio.sleep(0.05)


async def test_limit_is_shared_between_processes(redis_client):
    # Два объекта — как два процесса app: в памяти у каждого был бы свой запас
    first = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=2)
    second = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=2)

    assert [await limiter.allow(IP) for limiter in (first, second, first, second)] == [
        True,
        True,
        False,
        False,
    ]


async def test_concurrent_uploads_do_not_exceed_burst(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=5)
    allowed: list[bool] = []

    async def upload() -> None:
        allowed.append(await limiter.allow(IP))

    async with anyio.create_task_group() as group:
        for _ in range(20):
            group.start_soon(upload)

    assert allowed.count(True) == 5


async def test_bucket_expires_once_it_would_be_full(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=10, burst=2)

    await limiter.allow(IP)

    # Один токен потрачен: ведро наполнится за 0,1 с, ключ живёт ещё секунду запаса
    assert 0 < await redis_client.pttl(f"rl:upload:{IP}") <= 1100


async def test_unavailable_redis_falls_back_to_memory(unavailable_redis):
    limiter = UploadRateLimiter(unavailable_redis, rate=NO_REFILL, burst=2)
    started = time.perf_counter()

    # Загрузки не встают, но и без лимита не остаются
    assert [await limiter.allow(IP) for _ in range(3)] == [True, True, False]
    assert time.perf_counter() - started < 1
