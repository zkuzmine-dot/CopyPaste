import logging

from sqlalchemy.orm import sessionmaker

from app.db.connection import create_db_engine, create_session_factory
from app.services import notes as notes_service

logger = logging.getLogger(__name__)


def run_cleanup(session_factory: sessionmaker) -> int:
    # begin(): commit при успехе, rollback при ошибке — как get_db, только без FastAPI
    with session_factory() as db, db.begin():
        deleted = notes_service.delete_expired_notes(db)
    logger.log(logging.INFO if deleted else logging.DEBUG, "Deleted %d expired notes", deleted)
    return deleted


if __name__ == "__main__":
    # Разовый запуск: python -m app.jobs.cleanup
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    engine = create_db_engine()
    try:
        run_cleanup(create_session_factory(engine))
    finally:
        engine.dispose()
