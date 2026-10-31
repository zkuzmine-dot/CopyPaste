from fastapi import APIRouter, HTTPException

from app.db.connection import DbSession
from app.schemas.note import NoteRead, NoteUpdate
from app.services import notes as notes_service

router = APIRouter()

@router.get("/text", response_model=NoteRead)
def read_text(db: DbSession): # Это хуёво в маштабируемом проекте
    note = notes_service.get_note(db, 1)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note

@router.put("/text", response_model=NoteRead)
def update_text(payload: NoteUpdate, db: DbSession):
    return notes_service.upsert_note(db, 1, payload)
