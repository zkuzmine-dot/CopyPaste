import secrets
import time
from collections.abc import AsyncIterable, Iterable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

import anyio
from starlette.responses import FileResponse, Response

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

    def download(
        self,
        key: str,
        *,
        media_type: str,
        headers: Mapping[str, str],
    ) -> Response:
        path = self.path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return FileResponse(path, media_type=media_type, headers=headers)

    def open(self, key: str) -> BinaryIO:
        return self.path(key).open("rb")

    def write(self, key: str, chunks: Iterable[bytes]) -> None:
        target = self.path(key)
        self._tmp.mkdir(parents=True, exist_ok=True)
        part = self._tmp / f"{secrets.token_hex(16)}.part"
        try:
            with part.open("wb") as file:
                for chunk in chunks:
                    file.write(chunk)
            target.parent.mkdir(parents=True, exist_ok=True)
            part.replace(target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

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

    def delete_stale_partial_uploads(self, age: timedelta) -> int:
        # .part остаётся, только если процесс упал посреди загрузки: при обычной ошибке
        # или отмене его удаляет save. Время изменения обновляет каждая запись в файл,
        # поэтому идущую загрузку это не заденет
        cutoff = time.time() - age.total_seconds()
        deleted = 0
        for part in self._tmp.glob("*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
                    deleted += 1
            except FileNotFoundError:
                continue
        return deleted
