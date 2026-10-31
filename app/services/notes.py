from sqlalchemy.orm import Session

from app.db.models.note import Note
from app.schemas.note import NoteUpdate


def get_note(db: Session, note_id: int) -> Note | None:
    return db.get(Note, note_id)


def upsert_note(db: Session, note_id: int, payload: NoteUpdate) -> Note:
    note = db.get(Note, note_id)
    if note:
        if payload.text is not None:
            note.text = payload.text
    else:
        note = Note(id=note_id, text=payload.text or "")
        db.add(note)
    db.flush()
    db.refresh(note)
    return note
