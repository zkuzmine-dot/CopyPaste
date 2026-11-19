import logging
from datetime import timedelta
from itertools import batched

from sqlalchemy.orm import sessionmaker

from app.services import files as files_service
from app.storage.base import FileStorage

logger = logging.getLogger(__name__)

# Моложе часа не трогаем: строка в БД для только что записанного файла может ещё вставляться
ORPHAN_GRACE = timedelta(hours=1)
SWEEP_BATCH_SIZE = 500


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


def run_disk_sweep(
    session_factory: sessionmaker, storage: FileStorage, *, grace: timedelta = ORPHAN_GRACE
) -> tuple[int, int]:
    """Сверка диска с БД: байты без строки в БД и недописанные загрузки.

    Такое остаётся только после сбоя между шагами (порядок «диск, потом БД» при загрузке и
    «БД, потом диск» при удалении), поэтому хватает прохода раз в час.
    """
    orphans = 0
    for batch in batched(storage.keys_older_than(grace), SWEEP_BATCH_SIZE, strict=False):
        with session_factory() as db:
            known = files_service.known_keys(db, batch)
        for key in batch:
            if key not in known:
                storage.delete(key)
                orphans += 1
    parts = storage.delete_stale_partial_uploads(grace)
    logger.log(
        logging.INFO if orphans or parts else logging.DEBUG,
        "Disk sweep: deleted %d orphan files and %d partial uploads",
        orphans,
        parts,
    )
    return orphans, parts
