from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteUpdate
from app.services import notes as notes_service

pytestmark = pytest.mark.anyio


async def make_note(db, *, expires_at=None, text="hello") -> Note:
    # Напрямую через модель: схемы не дают создать заметку со сроком в прошлом
    note = Note(text=text, expires_at=expires_at)
    db.add(note)
    await db.flush()
    return note


def expired():
    return func.now() - timedelta(seconds=1)


def future():
    return datetime.now(UTC) + timedelta(hours=1)


async def slugs_in_db(db) -> set[str]:
    return set(await db.scalars(select(Note.slug)))


# --- create_note ---


async def test_create_note_saves_to_db(db):
    expires_at = future()

    note = await notes_service.create_note(db, NoteCreate(text="hello", expires_at=expires_at))

    assert note.id is not None
    assert note.slug
    assert note.created_at is not None
    assert note.expires_at == expires_at
    assert note.slug in await slugs_in_db(db)


async def test_create_note_generates_unique_slugs(db):
    first = await notes_service.create_note(db, NoteCreate(text="a"))
    second = await notes_service.create_note(db, NoteCreate(text="b"))

    assert first.slug != second.slug


# --- get_note_by_slug ---


async def test_get_note_by_slug(db):
    note = await make_note(db)

    assert await notes_service.get_note_by_slug(db, note.slug) is note


async def test_get_note_by_slug_with_future_expiry(db):
    note = await make_note(db, expires_at=future())

    assert await notes_service.get_note_by_slug(db, note.slug) is note


async def test_get_note_by_slug_unknown(db):
    assert await notes_service.get_note_by_slug(db, "zzzzzzzz") is None


async def test_get_note_by_slug_expired(db):
    note = await make_note(db, expires_at=expired())

    assert await notes_service.get_note_by_slug(db, note.slug) is None


# --- update_note ---


async def test_update_note_text(db):
    note = await make_note(db, expires_at=future())
    expires_at = note.expires_at

    await notes_service.update_note(db, note, NoteUpdate(text="updated"))

    assert note.text == "updated"
    assert note.expires_at == expires_at


async def test_update_note_only_expires_at_keeps_text(db):
    note = await make_note(db)
    expires_at = future()

    await notes_service.update_note(db, note, NoteUpdate(expires_at=expires_at))

    assert note.text == "hello"
    assert note.expires_at == expires_at


async def test_update_note_expires_at_none_removes_expiry(db):
    note = await make_note(db, expires_at=future())

    await notes_service.update_note(db, note, NoteUpdate(expires_at=None))

    assert note.expires_at is None


async def test_update_note_empty_payload_changes_nothing(db):
    note = await make_note(db, expires_at=future())
    before = (note.text, note.expires_at)

    await notes_service.update_note(db, note, NoteUpdate())

    assert (note.text, note.expires_at) == before


# --- delete_note ---


async def test_delete_note(db):
    note = await make_note(db)
    other = await make_note(db)

    await notes_service.delete_note(db, note)

    assert await slugs_in_db(db) == {other.slug}


# --- delete_expired_notes ---


async def test_delete_expired_notes(db):
    await make_note(db, expires_at=expired())
    await make_note(db, expires_at=expired())
    alive = await make_note(db, expires_at=future())
    forever = await make_note(db)

    deleted = await notes_service.delete_expired_notes(db)

    assert deleted == 2
    assert await slugs_in_db(db) == {alive.slug, forever.slug}


async def test_delete_expired_notes_nothing_to_delete(db):
    await make_note(db)

    assert await notes_service.delete_expired_notes(db) == 0
