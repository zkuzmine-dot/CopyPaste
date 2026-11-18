from datetime import timedelta

from sqlalchemy import func, select, update

from app.db.models.file import FileOrigin, StoredFile
from app.jobs.files import run_files_gc
from app.services import files as files_service
from app.storage.base import new_storage_key


def expired_file(db, storage_key: str) -> None:
    stored = files_service.create_upload(
        db,
        storage_key=storage_key,
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=4,
        origin=FileOrigin.NOTE,
    )
    db.execute(
        update(StoredFile)
        .where(StoredFile.id == stored.id)
        .values(keep_until=func.now() - timedelta(seconds=1))
    )


def test_run_files_gc_deletes_rows_and_bytes(db, session_factory, file_storage):
    key = new_storage_key()
    path = file_storage.path(key)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    expired_file(db, key)
    db.commit()

    assert run_files_gc(session_factory, file_storage) == 1
    assert not path.exists()
    assert db.scalar(select(func.count()).select_from(StoredFile)) == 0


def test_run_files_gc_continues_after_storage_error(db, session_factory, caplog):
    class BrokenStorage:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        def delete(self, key: str) -> None:
            self.attempts.append(key)
            raise OSError("disk error")

    storage = BrokenStorage()
    keys = {new_storage_key(), new_storage_key()}
    for key in keys:
        expired_file(db, key)
    db.commit()

    # Строки удалены в любом случае: оставшиеся байты найдёт сверка диска (этап 5)
    assert run_files_gc(session_factory, storage) == 2
    assert set(storage.attempts) == keys
    assert caplog.text.count("Failed to delete file") == 2
