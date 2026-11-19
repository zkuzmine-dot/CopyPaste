import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.db.models.file import FileOrigin, StoredFile
from app.db.models.note import Note
from app.schemas.note import NoteCreate
from app.services import files as files_service
from app.services import notes as notes_service
from app.storage.base import new_storage_key

# Настоящие коммиты: блокировки одной транзакции видны только из другой

RACE_WINDOW = 0.3


@pytest.fixture
def factory(engine):
    factory = sessionmaker(bind=engine)
    yield factory
    with factory.begin() as db:
        db.execute(delete(Note))
        db.execute(delete(StoredFile))


def committed_upload(factory) -> str:
    with factory.begin() as db:
        return files_service.create_upload(
            db,
            storage_key=new_storage_key(),
            name="a.txt",
            content_type="application/octet-stream",
            size_bytes=1,
            origin=FileOrigin.NOTE,
        ).public_id


def test_one_file_goes_to_only_one_of_two_notes(factory, monkeypatch):
    file_id = committed_upload(factory)

    # Первая транзакция привязала файл и держит блокировку строки до коммита.
    # Вторая ждёт, перепроверяет «файл свободен» и получает отказ
    original_attach = files_service.attach_to_note

    def slow_attach(db, note_id, public_ids):
        original_attach(db, note_id, public_ids)
        time.sleep(RACE_WINDOW)

    monkeypatch.setattr(files_service, "attach_to_note", slow_attach)
    barrier = threading.Barrier(2)

    def create_note() -> str:
        barrier.wait()
        try:
            with factory.begin() as db:
                return notes_service.create_note(db, NoteCreate(file_ids=[file_id])).slug
        except files_service.FilesNotAvailableError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create_note(), range(2)))

    assert results.count("rejected") == 1
    [winner] = [slug for slug in results if slug != "rejected"]
    with factory() as db:
        assert db.scalars(select(Note.slug)).all() == [winner]
        owner = db.scalar(select(StoredFile.note_id).where(StoredFile.public_id == file_id))
        assert owner == db.scalar(select(Note.id).where(Note.slug == winner))


# Так и задумано: DELETE дождался читателя и не нашёл строку — заметку уже удалило чтение
@pytest.mark.filterwarnings("ignore:DELETE statement on table 'notes'")
def test_burn_read_and_note_deletion_do_not_deadlock(factory, monkeypatch):
    file_id = committed_upload(factory)
    with factory.begin() as db:
        note = notes_service.create_note(
            db, NoteCreate(text="secret", burn_after_read=True, file_ids=[file_id])
        )
        slug, note_id = note.slug, note.id

    # Читатель пометил файлы и «думает». В это время приходит DELETE заметки:
    # он удаляет строку заметки, а затем обнуляет note_id у файлов. Если бы читатель
    # не заблокировал заметку первым, блокировки встретились бы — deadlock
    files_released = threading.Event()
    original_release = files_service.release_for_burn

    def slow_release(db, note_id):
        files = original_release(db, note_id)
        files_released.set()
        time.sleep(RACE_WINDOW)
        return files

    monkeypatch.setattr(files_service, "release_for_burn", slow_release)
    deleter_errors: list[Exception] = []

    def delete_note() -> None:
        try:
            with factory() as db:
                note = db.get(Note, note_id)
                files_released.wait(timeout=5)
                notes_service.delete_note(db, note)
                db.commit()
        except Exception as exc:
            deleter_errors.append(exc)

    deleter = threading.Thread(target=delete_note)
    deleter.start()
    try:
        with factory() as db:
            result = notes_service.read_note(db, slug)
            read_file_ids = [f.public_id for f in result.files] if result else None
            db.commit()
    finally:
        deleter.join(timeout=10)

    assert read_file_ids == [file_id]
    assert deleter_errors == []
