"""Лимит загрузок файлов по адресу — в Redis, общий для всех процессов app.

Если Redis недоступен, загрузки не останавливаются: лимит считается в памяти процесса,
как раньше. От Redis зависят только заметки с паролем — файлы и каналы работают без него.
"""

import logging

from redis import Redis
from redis.exceptions import RedisError

from app.realtime.rate_limit import KeyedRateLimiter

logger = logging.getLogger(__name__)

# То же ведро токенов, что TokenBucket, но целиком внутри Redis: проверка и списание —
# одна атомарная операция, параллельные загрузки не пройдут мимо лимита. Время — по часам
# Redis: у процессов app они могут расходиться
_TAKE_TOKEN = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1000000
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local tokens = tonumber(state[1]) or burst
local updated = tonumber(state[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - updated) * rate)
local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', string.format('%.6f', tokens),
           'updated', string.format('%.6f', now))
-- Полное ведро ничем не отличается от отсутствующего: ключ живёт, пока не наполнится
redis.call('PEXPIRE', KEYS[1], math.ceil((burst - tokens) / rate * 1000) + 1000)
return allowed
"""


def _bucket(key: str) -> str:
    return f"rl:upload:{key}"


class UploadRateLimiter:
    def __init__(self, redis: Redis, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = burst
        self._take = redis.register_script(_TAKE_TOKEN)
        self._fallback = KeyedRateLimiter(rate, burst)

    def allow(self, key: str) -> bool:
        try:
            return bool(self._take(keys=[_bucket(key)], args=[self._rate, self._burst]))
        except RedisError:
            logger.warning("Redis is unavailable: upload rate limit is counted in memory")
            return self._fallback.allow(key)
