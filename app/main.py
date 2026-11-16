from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.notes.routes import router as notes_router
from app.db.connection import create_db_engine, create_session_factory
from app.pages import STATIC_DIR
from app.pages import router as pages_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_db_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    yield
    engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(notes_router)
app.include_router(pages_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0")
