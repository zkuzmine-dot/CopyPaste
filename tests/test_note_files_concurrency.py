import anyio
import pytest
from sqlalchemy import select, text

from app.db.models.file import FileOrigin, StoredFile
from app.db.models.note import Note
from app.schemas.note import NoteCreate
from app.services import files as files_service
from app.services import notes as notes_service
from app.storage.base import new_storage_key

pytestmark = pytest.mark.anyio

# Настоящие коммиты: блокировки одной транзакции видны только из другой

RACE_WINDOW = 0.3


def hold(db) -> None:
    # Пауза на стороне БД: блокировки строк держатся, а цикл событий свободен —
    # другая задача успевает дойти до тех же строк
    db.execute(text("SELECT pg_sleep(:seconds)"), {"seconds": RACE_WINDOW})


@pytest.fixture
def factory(committing_session_factory):
    return committing_session_factory


async def committed_upload(factory) -> str:
    async with factory() as db, db.begin():
        stored = await db.run_sync(
            files_service.create_upload,
            storage_key=new_storage_key(),
            name="a.txt",
            content_type="application/octet-stream",
            size_bytes=1,
            origin=FileOrigin.NOTE,
        )
        return stored.public_id


async def test_one_file_goes_to_only_one_of_two_notes(factory, monkeypatch):
    file_id = await committed_upload(factory)

    # Первая транзакция привязала файл и держит блокировку строки до коммита.
    # Вторая ждёт, перепроверяет «файл свободен» и получает отказ
    original_attach = files_service.attach_to_note

    def slow_attach(db, note_id, public_ids, encrypted=None):
        original_attach(db, note_id, public_ids, encrypted)
        hold(db)

    monkeypatch.setattr(files_service, "attach_to_note", slow_attach)
    results: list[str] = []

    async def create_note() -> None:
        try:
            async with factory() as db, db.begin():
                note = await notes_service.create_note(db, NoteCreate(file_ids=[file_id]))
                results.append(note.slug)
        except files_service.FilesNotAvailableError:
            results.append("rejected")

    async with anyio.create_task_group() as group:
        group.start_soon(create_note)
        group.start_soon(create_note)

    assert results.count("rejected") == 1
    [winner] = [slug for slug in results if slug != "rejected"]
    async with factory() as db:
        assert (await db.scalars(select(Note.slug))).all() == [winner]
        owner = await db.scalar(select(StoredFile.note_id).where(StoredFile.public_id == file_id))
        assert owner == await db.scalar(select(Note.id).where(Note.slug == winner))


# Так и задумано: DELETE дождался читателя и не нашёл строку — заметку уже удалило чтение
@pytest.mark.filterwarnings("ignore:DELETE statement on table 'notes'")
async def test_burn_read_and_note_deletion_do_not_deadlock(factory, monkeypatch):
    file_id = await committed_upload(factory)
    async with factory() as db, db.begin():
        note = await notes_service.create_note(
            db, NoteCreate(text="secret", burn_after_read=True, file_ids=[file_id])
        )
        slug, note_id = note.slug, note.id

    # Читатель пометил файлы и «думает». В это время приходит DELETE заметки:
    # он удаляет строку заметки, а затем обнуляет note_id у файлов. Если бы читатель
    # не заблокировал заметку первым, блокировки встретились бы — deadlock
    files_released = anyio.Event()
    original_release = files_service.release_for_burn

    def slow_release(db, note_id):
        files = original_release(db, note_id)
        files_released.set()
        hold(db)
        return files

    monkeypatch.setattr(files_service, "release_for_burn", slow_release)
    deleter_errors: list[Exception] = []

    async def delete_note() -> None:
        try:
            async with factory() as db:
                note = await db.get(Note, note_id)
                await files_released.wait()
                await notes_service.delete_note(db, note)
                await db.commit()
        except Exception as exc:
            deleter_errors.append(exc)

    read_file_ids = None
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(delete_note)
            async with factory() as db:
                result = await notes_service.read_note(db, slug)
                read_file_ids = [f.public_id for f in result.files] if result else None
                await db.commit()

    assert read_file_ids == [file_id]
    assert deleter_errors == []
