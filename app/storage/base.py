import re
import secrets
from collections.abc import AsyncIterable, Iterator
from datetime import timedelta
from typing import Protocol

# ab/cd/abcd...: папки — первые символы самого имени. Только [0-9a-f] и "/",
# поэтому из ключа нельзя собрать путь за пределы хранилища
_KEY_PATTERN = re.compile(r"([0-9a-f]{2})/([0-9a-f]{2})/\1\2[0-9a-f]{28}")


class FileTooLargeError(Exception):
    pass


class InvalidStorageKeyError(ValueError):
    pass


def new_storage_key() -> str:
    name = secrets.token_hex(16)
    return f"{name[:2]}/{name[2:4]}/{name}"


def is_valid_key(key: str) -> bool:
    return _KEY_PATTERN.fullmatch(key) is not None


class FileStorage(Protocol):
    async def save(self, key: str, chunks: AsyncIterable[bytes], max_bytes: int) -> int:
        """Сохраняет поток целиком или не сохраняет ничего. Возвращает размер в байтах."""
        ...

    def delete(self, key: str) -> None: ...

    def keys_older_than(self, age: timedelta) -> Iterator[str]: ...
