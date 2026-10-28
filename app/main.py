from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from app.api.notes.routes import router as notes_router
from app.db.connection import create_db_engine, create_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_db_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    yield
    engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(notes_router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0")
