from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteUpdate
from app.services import notes as notes_service


def make_note(db, *, expires_at=None, text="hello") -> Note:
    # Напрямую через модель: схемы не дают создать заметку со сроком в прошлом
    note = Note(text=text, expires_at=expires_at)
    db.add(note)
    db.flush()
    return note


def expired():
    return func.now() - timedelta(seconds=1)


def future():
    return datetime.now(UTC) + timedelta(hours=1)


def slugs_in_db(db) -> set[str]:
    return set(db.scalars(select(Note.slug)))


# --- create_note ---


def test_create_note_saves_to_db(db):
    expires_at = future()

    note = notes_service.create_note(db, NoteCreate(text="hello", expires_at=expires_at))

    assert note.id is not None
    assert note.slug
    assert note.created_at is not None
    assert note.expires_at == expires_at
    assert note.slug in slugs_in_db(db)


def test_create_note_generates_unique_slugs(db):
    first = notes_service.create_note(db, NoteCreate(text="a"))
    second = notes_service.create_note(db, NoteCreate(text="b"))

    assert first.slug != second.slug


# --- get_note_by_slug ---


def test_get_note_by_slug(db):
    note = make_note(db)

    assert notes_service.get_note_by_slug(db, note.slug) is note


def test_get_note_by_slug_with_future_expiry(db):
    note = make_note(db, expires_at=future())

    assert notes_service.get_note_by_slug(db, note.slug) is note


def test_get_note_by_slug_unknown(db):
    assert notes_service.get_note_by_slug(db, "zzzzzzzz") is None


def test_get_note_by_slug_expired(db):
    note = make_note(db, expires_at=expired())

    assert notes_service.get_note_by_slug(db, note.slug) is None


# --- update_note ---


def test_update_note_text(db):
    note = make_note(db, expires_at=future())
    expires_at = note.expires_at

    notes_service.update_note(db, note, NoteUpdate(text="updated"))

    assert note.text == "updated"
    assert note.expires_at == expires_at


def test_update_note_only_expires_at_keeps_text(db):
    note = make_note(db)
    expires_at = future()

    notes_service.update_note(db, note, NoteUpdate(expires_at=expires_at))

    assert note.text == "hello"
    assert note.expires_at == expires_at


def test_update_note_expires_at_none_removes_expiry(db):
    note = make_note(db, expires_at=future())

    notes_service.update_note(db, note, NoteUpdate(expires_at=None))

    assert note.expires_at is None


def test_update_note_empty_payload_changes_nothing(db):
    note = make_note(db, expires_at=future())
    before = (note.text, note.expires_at)

    notes_service.update_note(db, note, NoteUpdate())

    assert (note.text, note.expires_at) == before


# --- delete_note ---


def test_delete_note(db):
    note = make_note(db)
    other = make_note(db)

    notes_service.delete_note(db, note)

    assert slugs_in_db(db) == {other.slug}


# --- delete_expired_notes ---


def test_delete_expired_notes(db):
    make_note(db, expires_at=expired())
    make_note(db, expires_at=expired())
    alive = make_note(db, expires_at=future())
    forever = make_note(db)

    deleted = notes_service.delete_expired_notes(db)

    assert deleted == 2
    assert slugs_in_db(db) == {alive.slug, forever.slug}


def test_delete_expired_notes_nothing_to_delete(db):
    make_note(db)

    assert notes_service.delete_expired_notes(db) == 0
