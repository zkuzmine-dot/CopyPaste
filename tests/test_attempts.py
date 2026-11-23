import time

import anyio
import pytest

from app.security.attempts import AttemptsUnavailableError, PasswordAttempts
from app.settings import settings

pytestmark = pytest.mark.anyio

IP, OTHER_IP = "203.0.113.7", "198.51.100.9"
SLUG, OTHER_SLUG = "abcd2345", "zzzz2345"


async def wait_for_expiry(redis_client, key: str, timeout: float = 3) -> None:
    # Не фиксированный sleep: Redis считает срок по системным часам, а их в Docker Desktop
    # подводит синхронизация — запаса в 100 мс иногда не хватало
    deadline = time.monotonic() + timeout
    while await redis_client.exists(key):
        assert time.monotonic() < deadline, f"{key} did not expire"
        await anyio.sleep(0.05)


@pytest.fixture
def attempts(redis_client) -> PasswordAttempts:
    return PasswordAttempts(redis_client)


@pytest.fixture
def limits(monkeypatch):
    def set_limits(**values: int) -> None:
        for name, value in values.items():
            monkeypatch.setattr(settings, f"UNLOCK_{name}", value)

    return set_limits


# --- пороги ---


async def test_nothing_is_frozen_initially(attempts):
    assert await attempts.retry_after(IP, SLUG) is None


async def test_note_locks_after_max_failures(attempts, limits):
    limits(NOTE_MAX_FAILURES=3, NOTE_LOCK_SECONDS=60, IP_MAX_FAILURES=100)

    assert [await attempts.record_failure(IP, SLUG) for _ in range(2)] == [2, 1]
    assert await attempts.retry_after(IP, SLUG) is None

    assert await attempts.record_failure(IP, SLUG) == 0
    assert 59 <= await attempts.retry_after(IP, SLUG) <= 60


async def test_note_lock_applies_to_every_ip(attempts, limits):
    limits(NOTE_MAX_FAILURES=2, IP_MAX_FAILURES=100)
    for _ in range(2):
        await attempts.record_failure(IP, SLUG)

    # Подбор с многих адресов упирается в блокировку самой заметки
    assert await attempts.retry_after(OTHER_IP, SLUG) is not None
    # А сам IP не заморожен: к другим заметкам с него можно
    assert await attempts.retry_after(IP, OTHER_SLUG) is None


async def test_ip_ban_applies_to_every_note(attempts, limits):
    limits(IP_MAX_FAILURES=3, IP_BAN_SECONDS=900, NOTE_MAX_FAILURES=100)
    for slug in ("note0001", "note0002", "note0003"):
        await attempts.record_failure(IP, slug)

    assert 899 <= await attempts.retry_after(IP, "untouched") <= 900
    assert await attempts.retry_after(OTHER_IP, "untouched") is None


async def test_remaining_is_the_closer_limit(attempts, limits):
    limits(NOTE_MAX_FAILURES=5, IP_MAX_FAILURES=2)

    # До блокировки заметки ещё 4, но IP заморозят уже на следующей ошибке
    assert await attempts.record_failure(IP, SLUG) == 1


async def test_success_resets_note_counter_but_not_ip(attempts, limits, redis_client):
    limits(NOTE_MAX_FAILURES=3, IP_MAX_FAILURES=10)
    for _ in range(2):
        await attempts.record_failure(IP, SLUG)

    await attempts.reset_note(SLUG)

    assert await redis_client.exists(f"pw:fail:note:{SLUG}") == 0
    # Верный пароль к своей заметке не отменяет подбор к чужим
    assert await redis_client.get(f"pw:fail:ip:{IP}") == b"2"


# --- сроки ---


async def test_counters_always_have_expiry(attempts, limits, redis_client):
    limits(NOTE_WINDOW_SECONDS=60, IP_WINDOW_SECONDS=900)

    await attempts.record_failure(IP, SLUG)

    assert 59 <= await redis_client.ttl(f"pw:fail:note:{SLUG}") <= 60
    assert 899 <= await redis_client.ttl(f"pw:fail:ip:{IP}") <= 900


async def test_failure_window_expires(attempts, limits, redis_client):
    limits(NOTE_MAX_FAILURES=2, NOTE_WINDOW_SECONDS=1, IP_MAX_FAILURES=100)
    await attempts.record_failure(IP, SLUG)

    await wait_for_expiry(redis_client, f"pw:fail:note:{SLUG}")

    # Окно прошло: старая ошибка забыта, новая заметку не блокирует
    assert await attempts.record_failure(IP, SLUG) == 1
    assert await attempts.retry_after(IP, SLUG) is None


async def test_lock_expires(attempts, limits, redis_client):
    limits(NOTE_MAX_FAILURES=1, NOTE_LOCK_SECONDS=1, IP_MAX_FAILURES=100)
    await attempts.record_failure(IP, SLUG)
    assert await attempts.retry_after(IP, SLUG) == 1

    await wait_for_expiry(redis_client, f"pw:lock:note:{SLUG}")

    assert await attempts.retry_after(IP, SLUG) is None


# --- гонка ---


async def test_concurrent_failures_are_all_counted(attempts, limits):
    limits(NOTE_MAX_FAILURES=20, IP_MAX_FAILURES=1000)
    remaining: list[int] = []

    async def fail() -> None:
        remaining.append(await attempts.record_failure(IP, SLUG))

    # Все 20 задач отправляют команды, пока остальные ждут ответа: попытки идут одновременно
    async with anyio.create_task_group() as group:
        for _ in range(20):
            group.start_soon(fail)

    # Каждая из 20 ошибок получила свой номер — ни одна не потерялась,
    # и двадцатая поставила блокировку
    assert sorted(remaining) == list(range(20))
    assert await attempts.retry_after(IP, SLUG) is not None


# --- Redis недоступен ---


async def test_unavailable_redis_is_an_error_not_a_pass(unavailable_redis):
    broken = PasswordAttempts(unavailable_redis)
    started = time.perf_counter()

    with pytest.raises(AttemptsUnavailableError):
        await broken.retry_after(IP, SLUG)
    with pytest.raises(AttemptsUnavailableError):
        await broken.record_failure(IP, SLUG)
    with pytest.raises(AttemptsUnavailableError):
        await broken.reset_note(SLUG)
    # И быстро: без многосекундных повторов, пока запрос ждёт ответа
    assert time.perf_counter() - started < 1
