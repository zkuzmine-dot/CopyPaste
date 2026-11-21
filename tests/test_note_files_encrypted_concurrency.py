import threading

import anyio
import pytest
from sqlalchemy import select

from app.db.models.file import StoredFile
from app.services import notes as notes_service
from app.settings import settings

pytestmark = pytest.mark.anyio

# Настоящие коммиты: блокировки одной транзакции видны только из другой


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(settings, "KDF_TIME_COST", 1)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 1024)
    monkeypatch.setattr(settings, "KDF_PARALLELISM", 1)


async def test_one_file_goes_to_only_one_of_two_protected_notes(
    committing_client, committing_session_factory, file_storage, monkeypatch
):
    client = committing_client
    uploaded = await client.post("/api/files", params={"name": "a.bin"}, content=b"x" * 300)
    file_id = uploaded.json()["id"]
    async with committing_session_factory() as db:
        plain_key = await db.scalar(
            select(StoredFile.storage_key).where(StoredFile.public_id == file_id)
        )

    # Оба запроса зашифровали файл раньше, чем кто-то из них его привязал.
    # Шифрование идёт в рабочих потоках — поэтому барьер потоковый
    barrier = threading.Barrier(2)
    original = notes_service.encrypt_uploads

    def encrypt_together(*args):
        encrypted = original(*args)
        barrier.wait(timeout=5)
        return encrypted

    monkeypatch.setattr(notes_service, "encrypt_uploads", encrypt_together)
    statuses = []

    async def create() -> None:
        body = {"file_ids": [file_id], "password": "correct horse"}
        statuses.append((await client.post("/api/notes", json=body)).status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(create)
        group.start_soon(create)

    assert sorted(statuses) == [201, 422]
    async with committing_session_factory() as db:
        stored = await db.scalar(select(StoredFile).where(StoredFile.public_id == file_id))
    assert stored is not None and stored.encryption_nonce is not None
    # На диске одна копия — та, на которую указывает строка. Исходник стёр победитель,
    # свою копию — проигравший
    on_disk = [p.relative_to(file_storage.root).as_posix() for p in file_storage.root.glob("*/*/*")]
    assert on_disk == [stored.storage_key]
    assert stored.storage_key != plain_key
