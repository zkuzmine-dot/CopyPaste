from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from app.api.channels.routes import router as channels_router
from app.api.channels.ws import router as channels_ws_router
from app.api.errors import validation_error_without_secrets
from app.api.files.routes import create_upload_rate_limiter
from app.api.files.routes import router as files_router
from app.api.notes.routes import router as notes_router
from app.db.connection import create_db_engine, create_session_factory
from app.db.redis import create_redis
from app.pages import STATIC_DIR
from app.pages import router as pages_router
from app.realtime.manager import ChannelManager
from app.settings import settings
from app.storage.local import LocalFileStorage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Соединения с БД берутся лениво, при первом запросе
    engine = create_db_engine()
    app.state.session_factory = create_session_factory(engine)
    app.state.channel_manager = ChannelManager()
    app.state.file_storage = LocalFileStorage(settings.FILES_DIR)
    # Соединение откроется при первой команде: без Redis приложение стартует,
    # а отказывают только операции, которым он нужен
    app.state.redis = create_redis(settings.REDIS_URL)
    app.state.upload_rate_limiter = create_upload_rate_limiter(app.state.redis)
    yield
    app.state.redis.close()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.add_exception_handler(RequestValidationError, validation_error_without_secrets)  # type: ignore[arg-type]
app.include_router(notes_router)
app.include_router(channels_router)
app.include_router(channels_ws_router)
app.include_router(files_router)
app.include_router(pages_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", ws_max_size=settings.WS_MAX_MESSAGE_BYTES)
