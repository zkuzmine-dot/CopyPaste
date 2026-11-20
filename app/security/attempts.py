"""Ограничение попыток ввода пароля к заметкам — в Redis.

Два независимых предела: заморозка IP останавливает одного атакующего на всех заметках,
блокировка заметки — подбор с многих адресов. Блокировка заметки короткая, чтобы атакующий
не мог надолго закрыть чужую заметку от владельца.
"""

import math

from redis import Redis
from redis.exceptions import RedisError

from app.settings import settings

# Одним скриптом, а не отдельными командами: Redis выполняет его целиком. Иначе счётчик мог
# остаться без срока (процесс упал между INCR и EXPIRE) или потерять параллельную попытку
_RECORD_FAILURE = """
local failures = redis.call('INCR', KEYS[1])
if failures == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
if failures >= tonumber(ARGV[2]) then
    redis.call('SET', KEYS[2], 1, 'EX', ARGV[3])
    redis.call('DEL', KEYS[1])
end
return failures
"""


class AttemptsUnavailableError(Exception):
    """Redis недоступен. Пароль тогда не проверяем: иначе подбор шёл бы без ограничений."""


def _failures_ip(ip: str) -> str:
    return f"pw:fail:ip:{ip}"


def _ban_ip(ip: str) -> str:
    return f"pw:ban:ip:{ip}"


def _failures_note(slug: str) -> str:
    return f"pw:fail:note:{slug}"


def _lock_note(slug: str) -> str:
    return f"pw:lock:note:{slug}"


class PasswordAttempts:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._record = redis.register_script(_RECORD_FAILURE)

    def retry_after(self, ip: str, slug: str) -> int | None:
        """Сколько секунд ждать, если IP или заметка заморожены; None — можно пробовать.

        Проверяется до Argon2: замороженный адрес не должен тратить память и CPU сервера.
        """
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.pttl(_ban_ip(ip))
            pipe.pttl(_lock_note(slug))
            remaining_ms = pipe.execute()
        except RedisError as exc:
            raise AttemptsUnavailableError from exc
        # PTTL: -2 — ключа нет. Округляем вверх, чтобы не сказать «через 0 секунд»
        waits = [math.ceil(ms / 1000) for ms in remaining_ms if ms > 0]
        return max(waits) if waits else None

    def record_failure(self, ip: str, slug: str) -> int:
        """Учитывает неверный пароль. Возвращает, сколько попыток осталось до заморозки."""
        try:
            note_failures = self._record(
                keys=[_failures_note(slug), _lock_note(slug)],
                args=[
                    settings.UNLOCK_NOTE_WINDOW_SECONDS,
                    settings.UNLOCK_NOTE_MAX_FAILURES,
                    settings.UNLOCK_NOTE_LOCK_SECONDS,
                ],
            )
            ip_failures = self._record(
                keys=[_failures_ip(ip), _ban_ip(ip)],
                args=[
                    settings.UNLOCK_IP_WINDOW_SECONDS,
                    settings.UNLOCK_IP_MAX_FAILURES,
                    settings.UNLOCK_IP_BAN_SECONDS,
                ],
            )
        except RedisError as exc:
            raise AttemptsUnavailableError from exc
        return max(
            min(
                settings.UNLOCK_NOTE_MAX_FAILURES - note_failures,
                settings.UNLOCK_IP_MAX_FAILURES - ip_failures,
            ),
            0,
        )

    def reset_note(self, slug: str) -> None:
        # Счётчик IP не сбрасываем: верный пароль к своей заметке не отменяет подбор к чужим
        try:
            self._redis.delete(_failures_note(slug))
        except RedisError as exc:
            raise AttemptsUnavailableError from exc
