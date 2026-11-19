from datetime import timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import sessionmaker

from app.db.models.file import FileOrigin, StoredFile
from app.services import files as files_service
from app.storage.base import new_storage_key

# Настоящие коммиты: блокировку одной транзакции видно только из другой


def test_collector_skips_rows_locked_by_another_transaction(engine):
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        stored = files_service.create_upload(
            db,
            storage_key=new_storage_key(),
            name="a.txt",
            content_type="application/octet-stream",
            size_bytes=1,
            origin=FileOrigin.NOTE,
        )
        db.execute(
            update(StoredFile)
            .where(StoredFile.id == stored.id)
            .values(keep_until=func.now() - timedelta(seconds=1))
        )
        file_id, key = stored.id, stored.storage_key

    try:
        with factory() as holder, holder.begin():
            # Так строку держит, например, привязка к заметке
            holder.execute(select(StoredFile.id).where(StoredFile.id == file_id).with_for_update())

            with factory() as collector, collector.begin():
                # Без SKIP LOCKED сборщик ждал бы освобождения строки и упал бы по таймауту
                collector.execute(text("SET LOCAL lock_timeout = '1s'"))
                assert files_service.collect_garbage(collector) == []

        with factory.begin() as db:
            assert files_service.collect_garbage(db) == [key]
    finally:
        with factory.begin() as db:
            db.execute(delete(StoredFile).where(StoredFile.id == file_id))
