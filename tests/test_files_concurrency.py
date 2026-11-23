from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.db.models.file import FileOrigin, StoredFile
from app.services import files as files_service
from app.storage.base import new_storage_key

pytestmark = pytest.mark.anyio

# Настоящие коммиты: блокировку одной транзакции видно только из другой


async def test_collector_skips_rows_locked_by_another_transaction(committing_session_factory):
    factory = committing_session_factory
    async with factory() as db, db.begin():
        stored = await files_service.create_upload(
            db,
            storage_key=new_storage_key(),
            name="a.txt",
            content_type="application/octet-stream",
            size_bytes=1,
            origin=FileOrigin.NOTE,
        )
        await db.execute(
            update(StoredFile)
            .where(StoredFile.id == stored.id)
            .values(keep_until=func.now() - timedelta(seconds=1))
        )
        file_id, key = stored.id, stored.storage_key

    async with factory() as holder, holder.begin():
        # Так строку держит, например, привязка к заметке
        await holder.execute(
            select(StoredFile.id).where(StoredFile.id == file_id).with_for_update()
        )

        async with factory() as collector, collector.begin():
            # Без SKIP LOCKED сборщик ждал бы освобождения строки и упал бы по таймауту
            await collector.execute(text("SET LOCAL lock_timeout = '1s'"))
            assert await files_service.collect_garbage(collector) == []

    async with factory() as db, db.begin():
        assert await files_service.collect_garbage(db) == [key]
