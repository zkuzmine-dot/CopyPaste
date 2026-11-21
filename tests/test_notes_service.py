from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteUpdate
from app.services import notes as notes_service

pytestmark = pytest.mark.anyio


async def make_note(async_db, *, expires_at=None, text="hello") -> Note:
    # Напрямую через модель: схемы не дают создать заметку со сроком в прошлом
    note = Note(text=text, expires_at=expires_at)
    async_db.add(note)
    await async_db.flush()
    return note


def expired():
    return func.now() - timedelta(seconds=1)


def future():
    return datetime.now(UTC) + timedelta(hours=1)


async def slugs_in_db(async_db) -> set[str]:
    return set(await async_db.scalars(select(Note.slug)))


# --- create_note ---


async def test_create_note_saves_to_db(async_db):
    expires_at = future()

    note = await notes_service.create_note(
        async_db, NoteCreate(text="hello", expires_at=expires_at)
    )

    assert note.id is not None
    assert note.slug
    assert note.created_at is not None
    assert note.expires_at == expires_at
    assert note.slug in await slugs_in_db(async_db)


async def test_create_note_generates_unique_slugs(async_db):
    first = await notes_service.create_note(async_db, NoteCreate(text="a"))
    second = await notes_service.create_note(async_db, NoteCreate(text="b"))

    assert first.slug != second.slug


# --- get_note_by_slug ---


async def test_get_note_by_slug(async_db):
    note = await make_note(async_db)

    assert await notes_service.get_note_by_slug(async_db, note.slug) is note


async def test_get_note_by_slug_with_future_expiry(async_db):
    note = await make_note(async_db, expires_at=future())

    assert await notes_service.get_note_by_slug(async_db, note.slug) is note


async def test_get_note_by_slug_unknown(async_db):
    assert await notes_service.get_note_by_slug(async_db, "zzzzzzzz") is None


async def test_get_note_by_slug_expired(async_db):
    note = await make_note(async_db, expires_at=expired())

    assert await notes_service.get_note_by_slug(async_db, note.slug) is None


# --- update_note ---


async def test_update_note_text(async_db):
    note = await make_note(async_db, expires_at=future())
    expires_at = note.expires_at

    await notes_service.update_note(async_db, note, NoteUpdate(text="updated"))

    assert note.text == "updated"
    assert note.expires_at == expires_at


async def test_update_note_only_expires_at_keeps_text(async_db):
    note = await make_note(async_db)
    expires_at = future()

    await notes_service.update_note(async_db, note, NoteUpdate(expires_at=expires_at))

    assert note.text == "hello"
    assert note.expires_at == expires_at


async def test_update_note_expires_at_none_removes_expiry(async_db):
    note = await make_note(async_db, expires_at=future())

    await notes_service.update_note(async_db, note, NoteUpdate(expires_at=None))

    assert note.expires_at is None


async def test_update_note_empty_payload_changes_nothing(async_db):
    note = await make_note(async_db, expires_at=future())
    before = (note.text, note.expires_at)

    await notes_service.update_note(async_db, note, NoteUpdate())

    assert (note.text, note.expires_at) == before


# --- delete_note ---


async def test_delete_note(async_db):
    note = await make_note(async_db)
    other = await make_note(async_db)

    await notes_service.delete_note(async_db, note)

    assert await slugs_in_db(async_db) == {other.slug}


# --- delete_expired_notes ---


async def test_delete_expired_notes(async_db):
    await make_note(async_db, expires_at=expired())
    await make_note(async_db, expires_at=expired())
    alive = await make_note(async_db, expires_at=future())
    forever = await make_note(async_db)

    deleted = await notes_service.delete_expired_notes(async_db)

    assert deleted == 2
    assert await slugs_in_db(async_db) == {alive.slug, forever.slug}


async def test_delete_expired_notes_nothing_to_delete(async_db):
    await make_note(async_db)

    assert await notes_service.delete_expired_notes(async_db) == 0
