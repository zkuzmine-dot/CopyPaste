import anyio
import pytest

from app.schemas.note import NoteCreate
from app.services import notes as notes_service
from app.settings import settings

pytestmark = pytest.mark.anyio

# Настоящие коммиты: блокировку одной транзакции видно только из другой


@pytest.fixture
def factory(committing_session_factory, monkeypatch):
    monkeypatch.setattr(settings, "KDF_TIME_COST", 1)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 1024)
    monkeypatch.setattr(settings, "KDF_PARALLELISM", 1)
    return committing_session_factory


async def test_two_correct_passwords_open_burn_note_once(factory):
    # Пароль проверяется вне транзакции, поэтому оба читателя могут его пройти.
    # Сжигание — уже в транзакции, под блокировкой заметки: текст получит один
    async with factory() as db, db.begin():
        note = await notes_service.create_note(
            db, NoteCreate(text="secret", password="correct horse", burn_after_read=True)
        )
        protected = await notes_service.protected_of(db, note)
    assert protected is not None

    result: dict = {}
    finished = anyio.Event()

    async def second_reader() -> None:
        async with factory() as db:
            result["opened"] = await notes_service.open_protected(db, protected)
            await db.commit()
        finished.set()

    async with factory() as first, anyio.create_task_group() as group:
        assert await notes_service.open_protected(first, protected) is not None

        group.start_soon(second_reader)
        await anyio.sleep(0.5)
        assert not finished.is_set(), "второй читатель должен ждать блокировку заметки"

        await first.commit()

    assert result["opened"] is None
