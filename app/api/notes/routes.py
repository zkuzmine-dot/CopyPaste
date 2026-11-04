from fastapi import APIRouter, HTTPException, status

from app.db.connection import DbSession
from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteRead, NoteUpdate
from app.services import notes as notes_service

router = APIRouter(prefix="/api/notes", tags=["notes"])


def get_note_or_404(db: DbSession, slug: str) -> Note:
    note = notes_service.get_note_by_slug(db, slug)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return note

@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
def create_note(payload: NoteCreate, db: DbSession):
    return notes_service.create_note(db, payload)

@router.get("/{slug}", response_model=NoteRead)
def read_note(slug: str, db: DbSession):
    return get_note_or_404(db, slug)

@router.put("/{slug}", response_model=NoteRead)
def update_note(slug: str, payload: NoteUpdate, db: DbSession):
    note = get_note_or_404(db, slug)
    return notes_service.update_note(db, note, payload)

@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(slug: str, db: DbSession):
    note = get_note_or_404(db, slug)
    notes_service.delete_note(db, note)
