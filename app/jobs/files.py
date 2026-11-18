import logging

from sqlalchemy.orm import sessionmaker

from app.services import files as files_service
from app.storage.base import FileStorage

logger = logging.getLogger(__name__)


def run_files_gc(session_factory: sessionmaker, storage: FileStorage) -> int:
    with session_factory() as db, db.begin():
        keys = files_service.collect_garbage(db)
    # Байты — только после коммита: если стереть раньше, а транзакция откатится,
    # в БД останется строка, указывающая на несуществующий файл
    for key in keys:
        try:
            storage.delete(key)
        except OSError:
            logger.exception("Failed to delete file %s", key)
    logger.log(logging.INFO if keys else logging.DEBUG, "Deleted %d unused files", len(keys))
    return len(keys)
