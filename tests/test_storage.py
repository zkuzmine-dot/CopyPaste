import os
import time
from datetime import timedelta

import anyio
import pytest

from app.storage.base import (
    FileTooLargeError,
    InvalidStorageKeyError,
    is_valid_key,
    new_storage_key,
)


async def stream(*chunks: bytes):
    for chunk in chunks:
        yield chunk


def part_files(storage) -> list:
    tmp = storage.root / "tmp"
    return list(tmp.glob("*.part")) if tmp.exists() else []


def make_old(path, seconds: int = 3600) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


# --- ключи ---


def test_new_storage_keys_are_valid_and_unique():
    keys = {new_storage_key() for _ in range(100)}

    assert len(keys) == 100
    assert all(is_valid_key(key) for key in keys)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../../etc/passwd",
        "ab/cd/../../../etc/passwd",
        "/ab/cd/abcd" + "0" * 28,
        "ab/cd/abcd" + "0" * 28 + "/x",
        "AB/CD/ABCD" + "0" * 28,
        "ab/cd/ef" + "0" * 30,
        "tmp/" + "0" * 32 + ".part",
    ],
    ids=[
        "empty",
        "traversal",
        "traversal_after_dirs",
        "absolute",
        "nested",
        "uppercase",
        "dirs_not_from_name",
        "temp_file",
    ],
)
def test_invalid_keys_are_rejected(file_storage, key):
    with pytest.raises(InvalidStorageKeyError):
        file_storage.path(key)
    with pytest.raises(InvalidStorageKeyError):
        file_storage.delete(key)


@pytest.mark.anyio
async def test_save_rejects_invalid_key(file_storage):
    with pytest.raises(InvalidStorageKeyError):
        await file_storage.save("../evil", stream(b"data"), max_bytes=100)

    assert part_files(file_storage) == []


# --- сохранение ---


@pytest.mark.anyio
async def test_save_and_read(file_storage):
    key = new_storage_key()

    size = await file_storage.save(key, stream(b"hello ", b"world"), max_bytes=100)

    assert size == 11
    assert file_storage.path(key).read_bytes() == b"hello world"
    assert part_files(file_storage) == []


@pytest.mark.anyio
async def test_file_of_exact_limit_is_saved(file_storage):
    key = new_storage_key()

    assert await file_storage.save(key, stream(b"x" * 6, b"x" * 4), max_bytes=10) == 10


@pytest.mark.anyio
async def test_too_large_file_is_not_saved(file_storage):
    key = new_storage_key()

    with pytest.raises(FileTooLargeError):
        await file_storage.save(key, stream(b"x" * 6, b"x" * 5), max_bytes=10)

    assert not file_storage.path(key).exists()
    assert part_files(file_storage) == []


@pytest.mark.anyio
async def test_file_is_not_visible_until_fully_written(file_storage):
    key = new_storage_key()
    visible_during_write = []

    async def slow_stream():
        yield b"first "
        visible_during_write.append(file_storage.path(key).exists())
        yield b"second"

    await file_storage.save(key, slow_stream(), max_bytes=100)

    assert visible_during_write == [False]
    assert file_storage.path(key).read_bytes() == b"first second"


@pytest.mark.anyio
async def test_broken_stream_leaves_nothing(file_storage):
    key = new_storage_key()

    async def broken_stream():
        yield b"partial"
        raise OSError("connection lost")

    with pytest.raises(OSError, match="connection lost"):
        await file_storage.save(key, broken_stream(), max_bytes=100)

    assert not file_storage.path(key).exists()
    assert part_files(file_storage) == []


@pytest.mark.anyio
async def test_cancelled_save_leaves_nothing(file_storage):
    # Так выглядит закрытая вкладка: загрузку отменяют посреди записи
    key = new_storage_key()
    first_chunk_written = anyio.Event()

    async def hanging_stream():
        yield b"partial"
        first_chunk_written.set()
        await anyio.sleep_forever()
        yield b"never"

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(file_storage.save, key, hanging_stream(), 100)
        await first_chunk_written.wait()
        assert len(part_files(file_storage)) == 1
        task_group.cancel_scope.cancel()

    assert not file_storage.path(key).exists()
    assert part_files(file_storage) == []


# --- удаление ---


@pytest.mark.anyio
async def test_delete(file_storage):
    key = new_storage_key()
    await file_storage.save(key, stream(b"data"), max_bytes=100)

    file_storage.delete(key)

    assert not file_storage.path(key).exists()


def test_delete_missing_key_is_ok(file_storage):
    file_storage.delete(new_storage_key())


# --- перечисление ---


@pytest.mark.anyio
async def test_keys_older_than(file_storage):
    old_key, fresh_key = new_storage_key(), new_storage_key()
    for key in (old_key, fresh_key):
        await file_storage.save(key, stream(b"data"), max_bytes=100)
    make_old(file_storage.path(old_key))
    # Не ключи хранилища: недописанный файл и посторонний файл в папке ключей
    part = file_storage.root / "tmp" / "abc.part"
    stray = file_storage.root / "ab" / "cd" / "not-a-key"
    stray.parent.mkdir(parents=True, exist_ok=True)
    for path in (part, stray):
        path.write_bytes(b"")
        make_old(path)

    assert list(file_storage.keys_older_than(timedelta(minutes=30))) == [old_key]


def test_keys_older_than_on_empty_storage(file_storage):
    assert list(file_storage.keys_older_than(timedelta(0))) == []


# --- недописанные загрузки ---


def test_delete_stale_partial_uploads(file_storage):
    tmp = file_storage.root / "tmp"
    tmp.mkdir(parents=True)
    old_part, fresh_part, not_a_part = tmp / "old.part", tmp / "fresh.part", tmp / "note.txt"
    for path in (old_part, fresh_part, not_a_part):
        path.write_bytes(b"x")
    make_old(old_part)
    make_old(not_a_part)

    assert file_storage.delete_stale_partial_uploads(timedelta(minutes=30)) == 1
    assert not old_part.exists()
    assert fresh_part.exists()
    assert not_a_part.exists()


def test_delete_stale_partial_uploads_on_empty_storage(file_storage):
    assert file_storage.delete_stale_partial_uploads(timedelta(0)) == 0
