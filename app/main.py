from fastapi import FastAPI
import uvicorn

from app.db.base import Base
from app.db.connection import engine
from app.db.models.note import Note
from app.api.notes.routes import router as notes_router

Base.metadata.create_all(engine)

app = FastAPI()
app.include_router(notes_router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0")
