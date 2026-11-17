import secrets
import time
from collections.abc import AsyncIterable, Iterator
from datetime import timedelta
from pathlib import Path

import anyio

from app.storage.base import FileTooLargeError, InvalidStorageKeyError, is_valid_key


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        # Внутри root, то есть на том же разделе диска: иначе переименование не атомарно
        self._tmp = root / "tmp"

    def path(self, key: str) -> Path:
        if not is_valid_key(key):
            raise InvalidStorageKeyError(key)
        return self.root / key

    async def save(self, key: str, chunks: AsyncIterable[bytes], max_bytes: int) -> int:
        target = anyio.Path(self.path(key))
        await anyio.Path(self._tmp).mkdir(parents=True, exist_ok=True)
        part = anyio.Path(self._tmp / f"{secrets.token_hex(16)}.part")
        try:
            size = await self._write(part, chunks, max_bytes)
            await target.parent.mkdir(parents=True, exist_ok=True)
            await part.replace(target)
        except BaseException:
            # BaseException и shield: при обрыве загрузки задачу отменяют, и без shield
            # удаление .part отменилось бы вместе с ней
            with anyio.CancelScope(shield=True):
                await part.unlink(missing_ok=True)
            raise
        return size

    @staticmethod
    async def _write(part: anyio.Path, chunks: AsyncIterable[bytes], max_bytes: int) -> int:
        size = 0
        file = await part.open("wb")
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLargeError
                await file.write(chunk)
        finally:
            with anyio.CancelScope(shield=True):
                await file.aclose()
        return size

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def keys_older_than(self, age: timedelta) -> Iterator[str]:
        cutoff = time.time() - age.total_seconds()
        for path in self.root.glob("*/*/*"):
            key = path.relative_to(self.root).as_posix()
            if not is_valid_key(key):
                continue
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime < cutoff:
                yield key
