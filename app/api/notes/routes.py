from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.connection import get_db
from app.schemas.note import NoteRead, NoteUpdate
from app.services import notes as notes_service

router = APIRouter()

@router.get("/text", response_model=NoteRead)
def read_text(db: Session = Depends(get_db)): # Это хуёво в маштабируемом проекте
    note = notes_service.get_note(db, 1)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note

@router.put("/text", response_model=NoteRead)
def update_text(payload: NoteUpdate, db: Session = Depends(get_db)):
    return notes_service.upsert_note(db, 1, payload)
