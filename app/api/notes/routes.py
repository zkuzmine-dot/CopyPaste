from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.connection import get_db
from app.db.models.note import Note

router = APIRouter()

@router.get("/text")
def read_text(db: Session = Depends(get_db)): # Это хуёво в маштабируемом проекте
    note = db.get(Note, 1)
    return {"text": note.text if note else ""}

@router.put("/text")
def update_text(new_text: str, db: Session = Depends(get_db)):
    note = db.get(Note, 1)  #Запихнуть в отдельную функцию,чтобы размер ручки был меньше
    if note:
        note.text = new_text
    else:
        note = Note(id=1, text=new_text)
        db.add(note)
    db.commit()
    return {"text": note.text} #Возвращать pydantic модель,чтобы не возвращать лишние поля
