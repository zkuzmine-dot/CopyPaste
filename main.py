import os
from fastapi import Depends, FastAPI
import uvicorn
from sqlalchemy import Text, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker,mapped_column, Mapped, Session

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")


Base.metadata.create_all(engine)

app = FastAPI()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/text")
def read_text(db: Session = Depends(get_db)):
    note = db.get(Note, 1)
    return {"text": note.text if note else ""}

@app.put("/text")
def update_text(new_text: str, db: Session = Depends(get_db)):
    note = db.get(Note, 1)
    if note:
        note.text = new_text
    else:
        note = Note(id=1, text=new_text)
        db.add(note)
    db.commit()
    return {"text": note.text}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0")