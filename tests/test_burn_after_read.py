from datetime import timedelta

import anyio
import pytest
from sqlalchemy import func, update

from app.db.models.note import Note
from app.schemas.note import NoteCreate
from app.services import notes as notes_service

pytestmark = pytest.mark.anyio

URL = "/api/notes"


@pytest.fixture
async def burn_slug(client) -> str:
    response = await client.post(URL, json={"text": "secret", "burn_after_read": True})
    assert response.status_code == 201
    assert response.json()["burn_after_read"] is True
    return response.json()["slug"]


async def test_get_does_not_burn_and_hides_text(client, burn_slug):
    first = await client.get(f"{URL}/{burn_slug}")
    second = await client.get(f"{URL}/{burn_slug}")

    assert first.status_code == 200
    assert first.json()["burn_after_read"] is True
    assert first.json()["text"] is None
    assert second.status_code == 200


async def test_read_burns_note(client, burn_slug):
    first = await client.post(f"{URL}/{burn_slug}/read")
    second = await client.post(f"{URL}/{burn_slug}/read")

    assert first.status_code == 200
    assert first.json()["text"] == "secret"
    assert second.status_code == 404
    assert (await client.get(f"{URL}/{burn_slug}")).status_code == 404


async def test_regular_note(client):
    slug = (await client.post(URL, json={"text": "hello"})).json()["slug"]

    assert (await client.get(f"{URL}/{slug}")).json()["text"] == "hello"
    assert (await client.post(f"{URL}/{slug}/read")).json()["text"] == "hello"
    assert (await client.post(f"{URL}/{slug}/read")).status_code == 200


async def test_qr_does_not_burn(client, burn_slug):
    assert (await client.get(f"{URL}/{burn_slug}/qr")).status_code == 200

    assert (await client.post(f"{URL}/{burn_slug}/read")).status_code == 200


async def test_read_unknown_and_expired(client, async_connection, burn_slug):
    await async_connection.execute(
        update(Note)
        .where(Note.slug == burn_slug)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert (await client.post(f"{URL}/{burn_slug}/read")).status_code == 404
    assert (await client.post(f"{URL}/zzzzzzzz/read")).status_code == 404


async def test_update_burn_note_forbidden(client, burn_slug):
    response = await client.put(f"{URL}/{burn_slug}", json={"text": "changed"})

    assert response.status_code == 409
    assert (await client.post(f"{URL}/{burn_slug}/read")).json()["text"] == "secret"


async def test_delete_burn_note(client, burn_slug):
    assert (await client.delete(f"{URL}/{burn_slug}")).status_code == 204
    assert (await client.get(f"{URL}/{burn_slug}")).status_code == 404


async def test_concurrent_reads_only_one_gets_note(committing_session_factory):
    # Настоящие транзакции с коммитом: изоляция через откат здесь не подходит,
    # нужны два независимых соединения
    factory = committing_session_factory
    async with factory() as db, db.begin():
        note = await notes_service.create_note(db, NoteCreate(text="secret", burn_after_read=True))
        slug = note.slug

    result: dict = {}
    finished = anyio.Event()

    async def second_reader() -> None:
        async with factory() as db:
            result["note"] = await notes_service.read_note(db, slug)
            await db.commit()
        finished.set()

    async with factory() as first, anyio.create_task_group() as group:
        assert await notes_service.read_note(first, slug) is not None  # удалил, но не закоммитил

        group.start_soon(second_reader)
        await anyio.sleep(0.5)
        assert not finished.is_set(), "второй читатель должен ждать блокировку строки"

        await first.commit()

    assert result["note"] is None
