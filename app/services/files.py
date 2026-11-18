from datetime import timedelta
from typing import cast

from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.db.models.file import FileOrigin, StoredFile
from app.settings import settings

GC_BATCH_SIZE = 500


def _has_owner() -> ColumnElement[bool]:
    return or_(StoredFile.note_id.is_not(None), StoredFile.message_id.is_not(None))


def _is_garbage() -> ColumnElement[bool]:
    # Явно, а не через NOT: keep_until = NULL дал бы в сравнении NULL, и NOT(...) тоже NULL —
    # такие файлы никогда не попали бы в сборщик
    return and_(
        StoredFile.note_id.is_(None),
        StoredFile.message_id.is_(None),
        or_(StoredFile.keep_until.is_(None), StoredFile.keep_until <= func.now()),
    )


def create_upload(
    db: Session,
    *,
    storage_key: str,
    name: str,
    content_type: str,
    size_bytes: int,
    origin: FileOrigin,
    uploaded_by_id: int | None = None,
) -> StoredFile:
    stored = StoredFile(
        storage_key=storage_key,
        name=name,
        content_type=content_type,
        size_bytes=size_bytes,
        origin=origin,
        uploaded_by_id=uploaded_by_id,
        keep_until=func.now() + timedelta(seconds=settings.PENDING_UPLOAD_TTL_SECONDS),
    )
    db.add(stored)
    db.flush()
    return stored


def total_size(db: Session) -> int:
    return cast(int, db.scalar(select(func.coalesce(func.sum(StoredFile.size_bytes), 0))))


def get_available_file(db: Session, public_id: str) -> StoredFile | None:
    stmt = select(StoredFile).where(
        StoredFile.public_id == public_id,
        or_(_has_owner(), StoredFile.keep_until > func.now()),
    )
    return db.execute(stmt).scalar_one_or_none()


def delete_file(db: Session, file_id: int) -> str | None:
    stmt = delete(StoredFile).where(StoredFile.id == file_id).returning(StoredFile.storage_key)
    return db.execute(stmt).scalar_one_or_none()


def collect_garbage(db: Session, limit: int = GC_BATCH_SIZE) -> list[str]:
    """Удаляет строки файлов без владельца и возвращает их ключи: байты стирает вызывающий."""
    # SKIP LOCKED: не ждём строки, которые прямо сейчас привязывают или удаляют
    garbage = (
        select(StoredFile.id).where(_is_garbage()).limit(limit).with_for_update(skip_locked=True)
    )
    stmt = (
        delete(StoredFile)
        .where(StoredFile.id.in_(garbage))
        .returning(StoredFile.storage_key)
        .execution_options(synchronize_session=False)
    )
    return list(db.scalars(stmt))
