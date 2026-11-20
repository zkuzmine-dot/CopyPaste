import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from app.db.redis import create_redis
from app.security.attempts import AttemptsUnavailableError, PasswordAttempts
from app.settings import settings

IP, OTHER_IP = "203.0.113.7", "198.51.100.9"
SLUG, OTHER_SLUG = "abcd2345", "zzzz2345"


def wait_for_expiry(redis_client, key: str, timeout: float = 3) -> None:
    # Не фиксированный sleep: Redis считает срок по системным часам, а их в Docker Desktop
    # подводит синхронизация — запаса в 100 мс иногда не хватало
    deadline = time.monotonic() + timeout
    while redis_client.exists(key):
        assert time.monotonic() < deadline, f"{key} did not expire"
        time.sleep(0.05)


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


def test_nothing_is_frozen_initially(attempts):
    assert attempts.retry_after(IP, SLUG) is None


def test_note_locks_after_max_failures(attempts, limits):
    limits(NOTE_MAX_FAILURES=3, NOTE_LOCK_SECONDS=60, IP_MAX_FAILURES=100)

    assert [attempts.record_failure(IP, SLUG) for _ in range(2)] == [2, 1]
    assert attempts.retry_after(IP, SLUG) is None

    assert attempts.record_failure(IP, SLUG) == 0
    assert 59 <= attempts.retry_after(IP, SLUG) <= 60


def test_note_lock_applies_to_every_ip(attempts, limits):
    limits(NOTE_MAX_FAILURES=2, IP_MAX_FAILURES=100)
    for _ in range(2):
        attempts.record_failure(IP, SLUG)

    # Подбор с многих адресов упирается в блокировку самой заметки
    assert attempts.retry_after(OTHER_IP, SLUG) is not None
    # А сам IP не заморожен: к другим заметкам с него можно
    assert attempts.retry_after(IP, OTHER_SLUG) is None


def test_ip_ban_applies_to_every_note(attempts, limits):
    limits(IP_MAX_FAILURES=3, IP_BAN_SECONDS=900, NOTE_MAX_FAILURES=100)
    for slug in ("note0001", "note0002", "note0003"):
        attempts.record_failure(IP, slug)

    assert 899 <= attempts.retry_after(IP, "untouched") <= 900
    assert attempts.retry_after(OTHER_IP, "untouched") is None


def test_remaining_is_the_closer_limit(attempts, limits):
    limits(NOTE_MAX_FAILURES=5, IP_MAX_FAILURES=2)

    # До блокировки заметки ещё 4, но IP заморозят уже на следующей ошибке
    assert attempts.record_failure(IP, SLUG) == 1


def test_success_resets_note_counter_but_not_ip(attempts, limits, redis_client):
    limits(NOTE_MAX_FAILURES=3, IP_MAX_FAILURES=10)
    for _ in range(2):
        attempts.record_failure(IP, SLUG)

    attempts.reset_note(SLUG)

    assert redis_client.exists(f"pw:fail:note:{SLUG}") == 0
    # Верный пароль к своей заметке не отменяет подбор к чужим
    assert redis_client.get(f"pw:fail:ip:{IP}") == b"2"


# --- сроки ---


def test_counters_always_have_expiry(attempts, limits, redis_client):
    limits(NOTE_WINDOW_SECONDS=60, IP_WINDOW_SECONDS=900)

    attempts.record_failure(IP, SLUG)

    assert 59 <= redis_client.ttl(f"pw:fail:note:{SLUG}") <= 60
    assert 899 <= redis_client.ttl(f"pw:fail:ip:{IP}") <= 900


def test_failure_window_expires(attempts, limits, redis_client):
    limits(NOTE_MAX_FAILURES=2, NOTE_WINDOW_SECONDS=1, IP_MAX_FAILURES=100)
    attempts.record_failure(IP, SLUG)

    wait_for_expiry(redis_client, f"pw:fail:note:{SLUG}")

    # Окно прошло: старая ошибка забыта, новая заметку не блокирует
    assert attempts.record_failure(IP, SLUG) == 1
    assert attempts.retry_after(IP, SLUG) is None


def test_lock_expires(attempts, limits, redis_client):
    limits(NOTE_MAX_FAILURES=1, NOTE_LOCK_SECONDS=1, IP_MAX_FAILURES=100)
    attempts.record_failure(IP, SLUG)
    assert attempts.retry_after(IP, SLUG) == 1

    wait_for_expiry(redis_client, f"pw:lock:note:{SLUG}")

    assert attempts.retry_after(IP, SLUG) is None


# --- гонка ---


def test_concurrent_failures_are_all_counted(attempts, limits):
    limits(NOTE_MAX_FAILURES=20, IP_MAX_FAILURES=1000)
    barrier = threading.Barrier(20)

    def fail(_) -> int:
        barrier.wait()
        return attempts.record_failure(IP, SLUG)

    with ThreadPoolExecutor(max_workers=20) as pool:
        remaining = list(pool.map(fail, range(20)))

    # Каждая из 20 ошибок получила свой номер — ни одна не потерялась,
    # и двадцатая поставила блокировку
    assert sorted(remaining) == list(range(20))
    assert attempts.retry_after(IP, SLUG) is not None


# --- Redis недоступен ---


def test_unavailable_redis_is_an_error_not_a_pass():
    # Тот же хост, но порт, на котором никого нет
    host = urlsplit(settings.REDIS_URL).hostname
    broken = PasswordAttempts(create_redis(f"redis://{host}:6390/0"))
    started = time.perf_counter()

    with pytest.raises(AttemptsUnavailableError):
        broken.retry_after(IP, SLUG)
    with pytest.raises(AttemptsUnavailableError):
        broken.record_failure(IP, SLUG)
    with pytest.raises(AttemptsUnavailableError):
        broken.reset_note(SLUG)
    # И быстро: без многосекундных повторов, пока запрос держит поток сервера
    assert time.perf_counter() - started < 1
