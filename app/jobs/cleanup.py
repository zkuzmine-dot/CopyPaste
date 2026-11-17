import logging

from sqlalchemy.orm import sessionmaker

from app.db.connection import create_db_engine, create_session_factory
from app.services import channels as channels_service
from app.services import notes as notes_service

logger = logging.getLogger(__name__)


def run_cleanup(session_factory: sessionmaker) -> int:
    # begin(): commit при успехе, rollback при ошибке — как get_db, только без FastAPI
    with session_factory() as db, db.begin():
        deleted = notes_service.delete_expired_notes(db)
    logger.log(logging.INFO if deleted else logging.DEBUG, "Deleted %d expired notes", deleted)
    return deleted


def run_channels_cleanup(session_factory: sessionmaker) -> tuple[int, int]:
    with session_factory() as db, db.begin():
        messages = channels_service.delete_expired_messages(db)
        channels = channels_service.delete_inactive_channels(db)
    logger.log(
        logging.INFO if messages or channels else logging.DEBUG,
        "Deleted %d expired messages and %d inactive channels",
        messages,
        channels,
    )
    return messages, channels


if __name__ == "__main__":
    # Разовый запуск: python -m app.jobs.cleanup
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    engine = create_db_engine()
    try:
        session_factory = create_session_factory(engine)
        run_cleanup(session_factory)
        run_channels_cleanup(session_factory)
    finally:
        engine.dispose()
