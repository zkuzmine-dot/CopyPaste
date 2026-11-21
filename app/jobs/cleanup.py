import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.connection import create_db_engine, create_session_factory
from app.services import channels as channels_service
from app.services import notes as notes_service

logger = logging.getLogger(__name__)


async def run_cleanup(session_factory: async_sessionmaker[AsyncSession]) -> int:
    # begin(): commit при успехе, rollback при ошибке — как get_db, только без FastAPI
    async with session_factory() as db, db.begin():
        deleted = await db.run_sync(notes_service.delete_expired_notes)
    logger.log(logging.INFO if deleted else logging.DEBUG, "Deleted %d expired notes", deleted)
    return deleted


async def run_channels_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    async with session_factory() as db, db.begin():
        messages = await db.run_sync(channels_service.delete_expired_messages)
        channels = await db.run_sync(channels_service.delete_inactive_channels)
    logger.log(
        logging.INFO if messages or channels else logging.DEBUG,
        "Deleted %d expired messages and %d inactive channels",
        messages,
        channels,
    )
    return messages, channels


async def _run_once() -> None:
    engine = create_db_engine()
    try:
        session_factory = create_session_factory(engine)
        await run_cleanup(session_factory)
        await run_channels_cleanup(session_factory)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    # Разовый запуск: python -m app.jobs.cleanup
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(_run_once())
