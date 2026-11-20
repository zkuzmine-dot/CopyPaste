import threading
import time

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from app.db.models.note import Note
from app.schemas.note import NoteCreate
from app.services import notes as notes_service
from app.settings import settings

# Настоящие коммиты: блокировку одной транзакции видно только из другой


@pytest.fixture
def factory(engine, monkeypatch):
    monkeypatch.setattr(settings, "KDF_TIME_COST", 1)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 1024)
    monkeypatch.setattr(settings, "KDF_PARALLELISM", 1)
    factory = sessionmaker(bind=engine)
    yield factory
    with factory.begin() as db:
        db.execute(delete(Note))


def test_two_correct_passwords_open_burn_note_once(factory):
    # Пароль проверяется вне транзакции, поэтому оба читателя могут его пройти.
    # Сжигание — уже в транзакции, под блокировкой заметки: текст получит один
    with factory.begin() as db:
        note = notes_service.create_note(
            db, NoteCreate(text="secret", password="correct horse", burn_after_read=True)
        )
        protected = notes_service.protected_of(db, note)
    assert protected is not None

    first = factory()
    result: dict = {}

    def second_reader() -> None:
        with factory() as db:
            result["opened"] = notes_service.open_protected(db, protected)
            db.commit()

    try:
        assert notes_service.open_protected(first, protected) is not None

        thread = threading.Thread(target=second_reader)
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive(), "второй читатель должен ждать блокировку заметки"

        first.commit()
        thread.join(timeout=5)
    finally:
        first.close()

    assert result["opened"] is None
