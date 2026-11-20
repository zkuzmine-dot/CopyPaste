import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from app.db.redis import create_redis
from app.security.upload_limit import UploadRateLimiter
from app.settings import settings

IP, OTHER_IP = "203.0.113.7", "198.51.100.9"
# Пополнение почти нулевое: проверяем только запас подряд
NO_REFILL = 0.001


def test_burst_then_refused(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=3)

    assert [limiter.allow(IP) for _ in range(4)] == [True, True, True, False]


def test_addresses_are_counted_separately(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=1)

    assert limiter.allow(IP)
    assert not limiter.allow(IP)
    assert limiter.allow(OTHER_IP)


def test_tokens_refill_over_time(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=10, burst=1)
    assert limiter.allow(IP)
    assert not limiter.allow(IP)

    time.sleep(0.15)

    assert limiter.allow(IP)


def test_limit_is_shared_between_processes(redis_client):
    # Два объекта — как два процесса app: в памяти у каждого был бы свой запас
    first = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=2)
    second = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=2)

    assert [first.allow(IP), second.allow(IP), first.allow(IP), second.allow(IP)] == [
        True,
        True,
        False,
        False,
    ]


def test_concurrent_uploads_do_not_exceed_burst(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=NO_REFILL, burst=5)
    barrier = threading.Barrier(20)

    def upload(_) -> bool:
        barrier.wait()
        return limiter.allow(IP)

    with ThreadPoolExecutor(max_workers=20) as pool:
        allowed = list(pool.map(upload, range(20)))

    assert allowed.count(True) == 5


def test_bucket_expires_once_it_would_be_full(redis_client):
    limiter = UploadRateLimiter(redis_client, rate=10, burst=2)

    limiter.allow(IP)

    # Один токен потрачен: ведро наполнится за 0,1 с, ключ живёт ещё секунду запаса
    assert 0 < redis_client.pttl(f"rl:upload:{IP}") <= 1100


def test_unavailable_redis_falls_back_to_memory():
    # Тот же хост, но порт, на котором никого нет
    host = urlsplit(settings.REDIS_URL).hostname
    limiter = UploadRateLimiter(create_redis(f"redis://{host}:6390/0"), rate=NO_REFILL, burst=2)
    started = time.perf_counter()

    # Загрузки не встают, но и без лимита не остаются
    assert [limiter.allow(IP) for _ in range(3)] == [True, True, False]
    assert time.perf_counter() - started < 1
