import logging
from datetime import timedelta
from itertools import islice

import anyio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import files as files_service
from app.storage.base import FileStorage

logger = logging.getLogger(__name__)

# Моложе часа не трогаем: строка в БД для только что записанного файла может ещё вставляться
ORPHAN_GRACE = timedelta(hours=1)
SWEEP_BATCH_SIZE = 500


def _delete_keys(storage: FileStorage, keys: list[str]) -> None:
    for key in keys:
        try:
            storage.delete(key)
        except OSError:
            logger.exception("Failed to delete file %s", key)


async def run_files_gc(
    session_factory: async_sessionmaker[AsyncSession], storage: FileStorage
) -> int:
    async with session_factory() as db, db.begin():
        keys = await files_service.collect_garbage(db)
    # Байты — только после коммита: если стереть раньше, а транзакция откатится,
    # в БД останется строка, указывающая на несуществующий файл. Диск — в потоке
    await anyio.to_thread.run_sync(_delete_keys, storage, keys)
    logger.log(logging.INFO if keys else logging.DEBUG, "Deleted %d unused files", len(keys))
    return len(keys)


async def run_disk_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    storage: FileStorage,
    *,
    grace: timedelta = ORPHAN_GRACE,
) -> tuple[int, int]:
    """Сверка диска с БД: байты без строки в БД и недописанные загрузки.

    Такое остаётся только после сбоя между шагами (порядок «диск, потом БД» при загрузке и
    «БД, потом диск» при удалении), поэтому хватает прохода раз в час.
    """
    keys = storage.keys_older_than(grace)
    orphans = 0
    # Обход диска — порциями в потоке: в цикле событий он остановил бы другие задачи
    while batch := await anyio.to_thread.run_sync(lambda: list(islice(keys, SWEEP_BATCH_SIZE))):
        async with session_factory() as db:
            known = await files_service.known_keys(db, batch)
        orphan_keys = [key for key in batch if key not in known]
        await anyio.to_thread.run_sync(_delete_keys, storage, orphan_keys)
        orphans += len(orphan_keys)
    parts = await anyio.to_thread.run_sync(storage.delete_stale_partial_uploads, grace)
    logger.log(
        logging.INFO if orphans or parts else logging.DEBUG,
        "Disk sweep: deleted %d orphan files and %d partial uploads",
        orphans,
        parts,
    )
    return orphans, parts
