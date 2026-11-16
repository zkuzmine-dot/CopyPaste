import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.orm import sessionmaker

from app.db.models.note import Note
from app.schemas.note import NoteCreate
from app.services import notes as notes_service

URL = "/api/notes"


@pytest.fixture
def burn_slug(client) -> str:
    response = client.post(URL, json={"text": "secret", "burn_after_read": True})
    assert response.status_code == 201
    assert response.json()["burn_after_read"] is True
    return response.json()["slug"]


def test_get_does_not_burn_and_hides_text(client, burn_slug):
    first = client.get(f"{URL}/{burn_slug}")
    second = client.get(f"{URL}/{burn_slug}")

    assert first.status_code == 200
    assert first.json()["burn_after_read"] is True
    assert first.json()["text"] is None
    assert second.status_code == 200


def test_read_burns_note(client, burn_slug):
    first = client.post(f"{URL}/{burn_slug}/read")
    second = client.post(f"{URL}/{burn_slug}/read")

    assert first.status_code == 200
    assert first.json()["text"] == "secret"
    assert second.status_code == 404
    assert client.get(f"{URL}/{burn_slug}").status_code == 404


def test_regular_note(client):
    slug = client.post(URL, json={"text": "hello"}).json()["slug"]

    assert client.get(f"{URL}/{slug}").json()["text"] == "hello"
    assert client.post(f"{URL}/{slug}/read").json()["text"] == "hello"
    assert client.post(f"{URL}/{slug}/read").status_code == 200


def test_qr_does_not_burn(client, burn_slug):
    assert client.get(f"{URL}/{burn_slug}/qr").status_code == 200

    assert client.post(f"{URL}/{burn_slug}/read").status_code == 200


def test_read_unknown_and_expired(client, connection, burn_slug):
    connection.execute(
        update(Note)
        .where(Note.slug == burn_slug)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert client.post(f"{URL}/{burn_slug}/read").status_code == 404
    assert client.post(f"{URL}/zzzzzzzz/read").status_code == 404


def test_update_burn_note_forbidden(client, burn_slug):
    response = client.put(f"{URL}/{burn_slug}", json={"text": "changed"})

    assert response.status_code == 409
    assert client.post(f"{URL}/{burn_slug}/read").json()["text"] == "secret"


def test_delete_burn_note(client, burn_slug):
    assert client.delete(f"{URL}/{burn_slug}").status_code == 204
    assert client.get(f"{URL}/{burn_slug}").status_code == 404


def test_concurrent_reads_only_one_gets_note(engine):
    # Настоящие транзакции с коммитом: изоляция через откат здесь не подходит,
    # нужны два независимых соединения
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        slug = notes_service.create_note(db, NoteCreate(text="secret", burn_after_read=True)).slug

    first = factory()
    result: dict = {}

    def second_reader() -> None:
        with factory() as db:
            result["note"] = notes_service.read_note(db, slug)
            db.commit()

    try:
        assert notes_service.read_note(first, slug) is not None  # удалил, но ещё не закоммитил

        thread = threading.Thread(target=second_reader)
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive(), "второй читатель должен ждать блокировку строки"

        first.commit()
        thread.join(timeout=5)
    finally:
        first.close()

    assert result["note"] is None
