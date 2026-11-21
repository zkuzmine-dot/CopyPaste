import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.file import FileOrigin, StoredFile
from app.jobs import files as files_job
from app.jobs.files import run_disk_sweep, run_files_gc
from app.services import files as files_service
from app.storage.base import new_storage_key

pytestmark = pytest.mark.anyio


async def expired_file(db, storage_key: str) -> None:
    stored = await files_service.create_upload(
        db,
        storage_key=storage_key,
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=4,
        origin=FileOrigin.NOTE,
    )
    await db.execute(
        update(StoredFile)
        .where(StoredFile.id == stored.id)
        .values(keep_until=func.now() - timedelta(seconds=1))
    )


async def test_run_files_gc_deletes_rows_and_bytes(db, session_factory, file_storage):
    key = new_storage_key()
    path = file_storage.path(key)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    await expired_file(db, key)
    await db.commit()

    assert await run_files_gc(session_factory, file_storage) == 1
    assert not path.exists()
    assert await db.scalar(select(func.count()).select_from(StoredFile)) == 0


async def test_run_files_gc_continues_after_storage_error(db, session_factory, caplog):
    class BrokenStorage:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        def delete(self, key: str) -> None:
            self.attempts.append(key)
            raise OSError("disk error")

    storage = BrokenStorage()
    keys = {new_storage_key(), new_storage_key()}
    for key in keys:
        await expired_file(db, key)
    await db.commit()

    # Строки удалены в любом случае: оставшиеся байты найдёт сверка диска (этап 5)
    assert await run_files_gc(session_factory, storage) == 2
    assert set(storage.attempts) == keys
    assert caplog.text.count("Failed to delete file") == 2


# --- сверка диска ---


def make_old(path) -> None:
    hour_and_minute_ago = time.time() - 3660
    os.utime(path, (hour_and_minute_ago, hour_and_minute_ago))


def blob(file_storage, *, old: bool, key: str | None = None):
    path = file_storage.path(key or new_storage_key())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if old:
        make_old(path)
    return path


async def test_disk_sweep(db, session_factory, file_storage):
    recorded_key = new_storage_key()
    recorded = blob(file_storage, old=True, key=recorded_key)
    await files_service.create_upload(
        db,
        storage_key=recorded_key,
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=1,
        origin=FileOrigin.NOTE,
    )
    await db.commit()
    orphan = blob(file_storage, old=True)
    # Без строки в БД, но свежий: строка могла ещё не успеть вставиться
    fresh_orphan = blob(file_storage, old=False)
    tmp = file_storage.root / "tmp"
    tmp.mkdir()
    old_part, fresh_part = tmp / "old.part", tmp / "fresh.part"
    old_part.write_bytes(b"x")
    fresh_part.write_bytes(b"x")
    make_old(old_part)

    assert await run_disk_sweep(session_factory, file_storage) == (1, 1)
    assert not orphan.exists()
    assert not old_part.exists()
    assert recorded.exists()
    assert fresh_orphan.exists()
    assert fresh_part.exists()


async def test_disk_sweep_in_batches(db, session_factory, file_storage, monkeypatch):
    monkeypatch.setattr(files_job, "SWEEP_BATCH_SIZE", 2)
    recorded_key = new_storage_key()
    blob(file_storage, old=True, key=recorded_key)
    await files_service.create_upload(
        db,
        storage_key=recorded_key,
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=1,
        origin=FileOrigin.NOTE,
    )
    await db.commit()
    orphans = [blob(file_storage, old=True) for _ in range(3)]

    assert await run_disk_sweep(session_factory, file_storage) == (3, 0)
    assert not any(path.exists() for path in orphans)
    assert file_storage.path(recorded_key).exists()
