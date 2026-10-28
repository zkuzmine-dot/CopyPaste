from fastapi import FastAPI
import uvicorn

from app.api.notes.routes import router as notes_router

app = FastAPI()
app.include_router(notes_router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0")
