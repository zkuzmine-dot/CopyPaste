"""Ключи файлов заметок с паролем на время после ввода пароля — в Redis.

Сервер не хранит DEK. После /unlock он кладётся сюда на FILE_KEY_CACHE_SECONDS, а ссылки на
файлы несут только id записи: сам ключ не попадает ни в адрес, ни в логи. Срок вышел —
заметку открывают паролем заново.
"""

import secrets
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.settings import settings


class FileKeysUnavailableError(Exception):
    pass


def _name(key_id: str) -> str:
    return f"file-key:{key_id}"


class FileKeys:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def put(self, key: bytes) -> tuple[str, int]:
        """id записи и момент (unix), когда она исчезнет: ссылки не должны жить дольше."""
        key_id = secrets.token_urlsafe(16)
        expires = int(time.time()) + settings.FILE_KEY_CACHE_SECONDS
        try:
            await self._redis.set(_name(key_id), key, exat=expires)
        except RedisError as exc:
            raise FileKeysUnavailableError from exc
        return key_id, expires

    async def get(self, key_id: str) -> bytes | None:
        try:
            value = await self._redis.get(_name(key_id))
        except RedisError as exc:
            raise FileKeysUnavailableError from exc
        return value if isinstance(value, bytes) else None
