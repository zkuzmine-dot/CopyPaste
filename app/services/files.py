from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import Channel, Message
from app.db.models.file import FileOrigin, StoredFile
from app.db.models.note import Note
from app.settings import settings

GC_BATCH_SIZE = 500


class FilesNotAvailableError(Exception):
    """Часть файлов не найдена, уже привязана или просрочена. Транзакцию нужно откатить."""


@dataclass(frozen=True)
class DeletedFile:
    storage_key: str
    message_id: int | None
    channel_slug: str | None


@dataclass(frozen=True)
class EncryptedUpload:
    """Зашифрованная копия загрузки для заметки с паролем."""

    storage_key: str
    nonce: bytes
    chunk_size: int


def _owner_is_alive() -> ColumnElement[bool]:
    # Истёкшие заметка и сообщение живут в БД до прохода чистки — их файлы отдавать уже нельзя
    note_alive = (
        select(Note.id)
        .where(
            Note.id == StoredFile.note_id,
            or_(Note.expires_at.is_(None), Note.expires_at > func.now()),
        )
        .exists()
    )
    message_alive = (
        select(Message.id)
        .where(Message.id == StoredFile.message_id, Message.expires_at > func.now())
        .exists()
    )
    return or_(note_alive, message_alive)


def _is_garbage() -> ColumnElement[bool]:
    # Явно, а не через NOT: keep_until = NULL дал бы в сравнении NULL, и NOT(...) тоже NULL —
    # такие файлы никогда не попали бы в сборщик
    return and_(
        StoredFile.note_id.is_(None),
        StoredFile.message_id.is_(None),
        or_(StoredFile.keep_until.is_(None), StoredFile.keep_until <= func.now()),
    )


async def create_upload(
    db: AsyncSession,
    *,
    storage_key: str,
    name: str,
    content_type: str,
    size_bytes: int,
    origin: FileOrigin,
    uploaded_by_id: int | None = None,
) -> StoredFile:
    # INSERT ... RETURNING: срок считает БД и сразу возвращает его в объекте (как у сообщений)
    stmt = (
        insert(StoredFile)
        .values(
            storage_key=storage_key,
            name=name,
            content_type=content_type,
            size_bytes=size_bytes,
            origin=origin,
            uploaded_by_id=uploaded_by_id,
            keep_until=func.now() + timedelta(seconds=settings.PENDING_UPLOAD_TTL_SECONDS),
        )
        .returning(StoredFile)
    )
    return (await db.scalars(stmt)).one()


async def total_size(db: AsyncSession) -> int:
    return cast(int, await db.scalar(select(func.coalesce(func.sum(StoredFile.size_bytes), 0))))


async def known_keys(db: AsyncSession, storage_keys: Sequence[str]) -> set[str]:
    stmt = select(StoredFile.storage_key).where(StoredFile.storage_key.in_(storage_keys))
    return set(await db.scalars(stmt))


async def get_available_file(db: AsyncSession, public_id: str) -> StoredFile | None:
    stmt = select(StoredFile).where(
        StoredFile.public_id == public_id,
        or_(_owner_is_alive(), StoredFile.keep_until > func.now()),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _is_free_note_upload(public_ids: Sequence[str]) -> ColumnElement[bool]:
    return and_(
        StoredFile.public_id.in_(public_ids),
        StoredFile.origin == FileOrigin.NOTE,
        StoredFile.note_id.is_(None),
        StoredFile.message_id.is_(None),
        StoredFile.keep_until > func.now(),
    )


async def pending_uploads(db: AsyncSession, public_ids: Sequence[str]) -> dict[str, str]:
    """Ключи хранилища загрузок, которые можно привязать к заметке, — чтобы их зашифровать.

    Это только предварительная проверка: окончательная — в attach_to_note.
    """
    stmt = select(StoredFile.public_id, StoredFile.storage_key).where(
        _is_free_note_upload(public_ids)
    )
    keys = {public_id: key for public_id, key in await db.execute(stmt)}
    if len(keys) != len(public_ids):
        raise FilesNotAvailableError
    return keys


async def attach_to_note(
    db: AsyncSession,
    note_id: int,
    public_ids: Sequence[str],
    encrypted: Mapping[str, EncryptedUpload] | None = None,
) -> None:
    """encrypted — копии для заметки с паролем: строка переходит на них в той же транзакции."""
    if not public_ids:
        return
    positions = {public_id: i for i, public_id in enumerate(public_ids)}
    # Все условия «файл свободен» — в самом UPDATE: если два запроса привязывают один файл,
    # второй дождётся блокировки строки, перепроверит условия и файл уже не получит
    stmt = (
        update(StoredFile)
        .where(_is_free_note_upload(public_ids))
        .values(
            note_id=note_id,
            keep_until=None,
            position=case(positions, value=StoredFile.public_id),
        )
    )
    if encrypted is not None:
        assert set(encrypted) == set(public_ids)
        items = encrypted.items()
        stmt = stmt.values(
            storage_key=case(
                {public_id: upload.storage_key for public_id, upload in items},
                value=StoredFile.public_id,
            ),
            encryption_nonce=case(
                {public_id: upload.nonce for public_id, upload in items},
                value=StoredFile.public_id,
            ),
            chunk_size=case(
                {public_id: upload.chunk_size for public_id, upload in items},
                value=StoredFile.public_id,
            ),
        )
    attached = stmt.returning(StoredFile.id).execution_options(synchronize_session=False)
    if len((await db.scalars(attached)).all()) != len(public_ids):
        raise FilesNotAvailableError


async def has_encrypted_files(db: AsyncSession, note_id: int) -> bool:
    stmt = select(
        exists().where(StoredFile.note_id == note_id, StoredFile.encryption_nonce.is_not(None))
    )
    return bool(await db.scalar(stmt))


async def files_of_note(db: AsyncSession, note_id: int) -> Sequence[StoredFile]:
    stmt = select(StoredFile).where(StoredFile.note_id == note_id).order_by(StoredFile.position)
    return (await db.scalars(stmt)).all()


async def release_for_burn(db: AsyncSession, note_id: int) -> list[StoredFile]:
    """Перед удалением одноразовой заметки: файлы живут ещё окно и удаляются после скачивания."""
    window = timedelta(seconds=settings.BURN_NOTE_FILES_WINDOW_SECONDS)
    stmt = (
        update(StoredFile)
        .where(StoredFile.note_id == note_id)
        .values(keep_until=func.now() + window, delete_after_download=True)
        .returning(StoredFile)
    )
    return sorted((await db.scalars(stmt)).all(), key=lambda stored: stored.position or 0)


async def delete_received(db: AsyncSession, public_id: str) -> DeletedFile | None:
    """Клиент получил одноразовый файл: удаляем строку, а у файла канала — и его сообщение.

    None, если файл не одноразовый или его уже удалили: тогда и сообщать никому не нужно.
    """
    stmt = (
        delete(StoredFile)
        .where(StoredFile.public_id == public_id, StoredFile.delete_after_download.is_(True))
        .returning(StoredFile.storage_key, StoredFile.message_id)
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None
    channel_slug = None
    if row.message_id is not None:
        channel_slug = await db.scalar(
            select(Channel.slug)
            .join(Message, Message.channel_id == Channel.id)
            .where(Message.id == row.message_id)
        )
        await db.execute(delete(Message).where(Message.id == row.message_id))
    return DeletedFile(row.storage_key, row.message_id, channel_slug)


async def attach_to_message(
    db: AsyncSession, message_id: int, public_id: str, *, uploaded_by_id: int
) -> StoredFile:
    """FilesNotAvailableError — транзакцию нужно откатить."""
    # Как и с заметками, все условия — в самом UPDATE. Плюс файл должен быть своим:
    # загружен этим участником и именно для канала
    stmt = (
        update(StoredFile)
        .where(
            StoredFile.public_id == public_id,
            StoredFile.origin == FileOrigin.CHANNEL,
            StoredFile.uploaded_by_id == uploaded_by_id,
            StoredFile.note_id.is_(None),
            StoredFile.message_id.is_(None),
            StoredFile.keep_until > func.now(),
        )
        .values(message_id=message_id, keep_until=None, delete_after_download=True)
        .returning(StoredFile)
    )
    stored = (await db.scalars(stmt)).one_or_none()
    if stored is None:
        raise FilesNotAvailableError
    return stored


async def files_of_messages(db: AsyncSession, message_ids: Sequence[int]) -> dict[int, StoredFile]:
    if not message_ids:
        return {}
    stmt = select(StoredFile).where(StoredFile.message_id.in_(message_ids))
    found = await db.scalars(stmt)
    return {stored.message_id: stored for stored in found if stored.message_id}


async def collect_garbage(db: AsyncSession, limit: int = GC_BATCH_SIZE) -> list[str]:
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
    return list(await db.scalars(stmt))
