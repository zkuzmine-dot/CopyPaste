import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.api.files.routes import create_upload_rate_limiter
from app.db.models.file import StoredFile
from app.db.models.note import Note
from app.main import app
from app.services import notes as notes_service
from app.settings import settings
from app.storage.local import LocalFileStorage

# Настоящие коммиты: блокировки одной транзакции видны только из другой


@pytest.fixture
def real_app(engine, tmp_path, redis_client, monkeypatch):
    monkeypatch.setattr(settings, "KDF_TIME_COST", 1)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 1024)
    monkeypatch.setattr(settings, "KDF_PARALLELISM", 1)
    factory = sessionmaker(bind=engine)
    storage = LocalFileStorage(tmp_path / "files")
    app.state.session_factory = factory
    app.state.file_storage = storage
    app.state.upload_rate_limiter = create_upload_rate_limiter(redis_client)
    app.state.redis = redis_client
    yield factory, storage
    del app.state.session_factory
    del app.state.file_storage
    del app.state.upload_rate_limiter
    del app.state.redis
    with factory.begin() as db:
        db.execute(delete(Note))
        db.execute(delete(StoredFile))


def test_one_file_goes_to_only_one_of_two_protected_notes(real_app, monkeypatch):
    factory, storage = real_app
    uploaded = TestClient(app).post("/api/files", params={"name": "a.bin"}, content=b"x" * 300)
    file_id = uploaded.json()["id"]
    with factory() as db:
        plain_key = db.scalar(select(StoredFile.storage_key).where(StoredFile.public_id == file_id))

    # Оба запроса зашифровали файл раньше, чем кто-то из них его привязал
    barrier = threading.Barrier(2)
    original = notes_service.encrypt_uploads

    def encrypt_together(*args):
        encrypted = original(*args)
        barrier.wait(timeout=5)
        return encrypted

    monkeypatch.setattr(notes_service, "encrypt_uploads", encrypt_together)

    def create(_) -> int:
        body = {"file_ids": [file_id], "password": "correct horse"}
        return TestClient(app).post("/api/notes", json=body).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(create, range(2)))

    assert statuses == [201, 422]
    with factory() as db:
        stored = db.scalar(select(StoredFile).where(StoredFile.public_id == file_id))
    assert stored is not None and stored.encryption_nonce is not None
    # На диске одна копия — та, на которую указывает строка. Исходник стёр победитель,
    # свою копию — проигравший
    on_disk = [p.relative_to(storage.root).as_posix() for p in storage.root.glob("*/*/*")]
    assert on_disk == [stored.storage_key]
    assert stored.storage_key != plain_key
