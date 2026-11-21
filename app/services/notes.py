from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.file import StoredFile
from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteUpdate
from app.services import files as files_service
from app.settings import settings
from app.storage.base import FileStorage
from app.storage.encrypted import encrypt_upload
from app.utils import crypto
from app.utils.slug import generate_slug


@dataclass(frozen=True)
class NoteWithFiles:
    note: Note
    files: Sequence[StoredFile]


@dataclass(frozen=True)
class ProtectedNote:
    """Всё, что нужно для проверки пароля, — чтобы Argon2 считался без открытой сессии БД."""

    id: int
    slug: str
    encrypted: crypto.EncryptedNote
    # Ключ тогда нужен и после ввода пароля — чтобы отдавать файлы
    has_encrypted_files: bool = False


@dataclass(frozen=True)
class PreparedNote:
    """Заметка до записи в БД. У заметки с паролем Argon2 уже посчитан."""

    slug: str
    encrypted: crypto.EncryptedNote | None = None
    # DEK — им же шифруются файлы заметки
    key: bytes | None = None


def _prepare(payload: NoteCreate) -> PreparedNote:
    slug = generate_slug()
    if payload.password is None:
        return PreparedNote(slug)
    key = crypto.new_note_key()
    encrypted = crypto.encrypt_note(payload.password.get_secret_value(), payload.text, slug, key)
    return PreparedNote(slug, encrypted, key)


async def prepare_note(payload: NoteCreate) -> PreparedNote:
    # С паролем — в потоке: Argon2 (~0,1 с процессора) не должен останавливать цикл событий
    if payload.password is None:
        return _prepare(payload)
    return await crypto.in_kdf_thread(_prepare, payload)


def encrypt_uploads(
    storage: FileStorage, uploads: Mapping[str, str], key: bytes
) -> dict[str, files_service.EncryptedUpload]:
    """Зашифрованные копии загрузок {public_id: ключ хранилища}. Без сессии БД.

    FilesNotAvailableError, если байтов загрузки уже нет. При любой ошибке записанные
    копии стираются.
    """
    encrypted: dict[str, files_service.EncryptedUpload] = {}
    try:
        for public_id, source_key in uploads.items():
            cipher = crypto.FileCipher(
                key=key,
                nonce=crypto.new_file_nonce(),
                file_id=public_id,
                chunk_size=settings.FILE_ENCRYPTION_CHUNK_BYTES,
            )
            target = encrypt_upload(storage, source_key, cipher)
            encrypted[public_id] = files_service.EncryptedUpload(
                target, cipher.nonce, cipher.chunk_size
            )
    except FileNotFoundError:
        delete_copies(storage, encrypted)
        raise files_service.FilesNotAvailableError from None
    except BaseException:
        delete_copies(storage, encrypted)
        raise
    return encrypted


def delete_copies(
    storage: FileStorage, encrypted: Mapping[str, files_service.EncryptedUpload]
) -> None:
    for upload in encrypted.values():
        storage.delete(upload.storage_key)


async def create_note(
    db: AsyncSession,
    payload: NoteCreate,
    *,
    prepared: PreparedNote | None = None,
    encrypted_files: Mapping[str, files_service.EncryptedUpload] | None = None,
) -> Note:
    """FilesNotAvailableError, если часть файлов привязать нельзя: транзакцию нужно откатить.

    prepared и encrypted_files готовят заранее, чтобы Argon2 и шифрование файлов шли без
    открытой транзакции. Без prepared Argon2 считается здесь, до первого запроса к БД.
    """
    if prepared is None:
        prepared = await prepare_note(payload)
    note = Note(
        slug=prepared.slug,
        text=payload.text,
        expires_at=payload.expires_at,
        burn_after_read=payload.burn_after_read,
    )
    if prepared.encrypted is not None:
        encrypted = prepared.encrypted
        note.text = ""
        note.kdf_salt = encrypted.kdf.salt
        note.kdf_time_cost = encrypted.kdf.time_cost
        note.kdf_memory_kib = encrypted.kdf.memory_kib
        note.kdf_parallelism = encrypted.kdf.parallelism
        note.wrapped_key = encrypted.wrapped_key
        note.ciphertext = encrypted.ciphertext
    db.add(note)
    # id и created_at приходят из INSERT ... RETURNING, refresh не нужен
    await db.flush()
    # Сервис файлов пока синхронный — через мост run_sync
    await db.run_sync(files_service.attach_to_note, note.id, payload.file_ids, encrypted_files)
    return note


async def get_note_by_slug(db: AsyncSession, slug: str) -> Note | None:
    # Протухшие заметки считаем несуществующими
    stmt = select(Note).where(
        Note.slug == slug,
        or_(Note.expires_at.is_(None), Note.expires_at > func.now()),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _lock_note(db: AsyncSession, note_id: int) -> bool:
    stmt = select(Note.id).where(Note.id == note_id).with_for_update()
    return (await db.execute(stmt)).first() is not None


async def _open(db: AsyncSession, note: Note) -> NoteWithFiles | None:
    if not note.burn_after_read:
        return NoteWithFiles(note, await db.run_sync(files_service.files_of_note, note.id))
    # Сначала блокируем заметку, потом файлы — в том же порядке, что и DELETE заметки
    # (сама строка, потом SET NULL у файлов). Иначе встречные блокировки дали бы deadlock.
    # Второй одновременный читатель дождётся блокировки и строки уже не найдёт
    if not await _lock_note(db, note.id):
        return None
    files = await db.run_sync(files_service.release_for_burn, note.id)
    await db.execute(
        delete(Note).where(Note.id == note.id).execution_options(synchronize_session=False)
    )
    return NoteWithFiles(note, files)


async def read_note(db: AsyncSession, slug: str) -> NoteWithFiles | None:
    note = await get_note_by_slug(db, slug)
    if note is None:
        return None
    return await _open(db, note)


async def protected_of(db: AsyncSession, note: Note) -> ProtectedNote | None:
    if not note.has_password:
        return None
    # Колонки заданы все вместе — это гарантирует CHECK ck_notes_encryption_complete
    assert note.kdf_salt is not None and note.wrapped_key is not None
    assert note.ciphertext is not None
    kdf = crypto.KdfParams(
        salt=note.kdf_salt,
        time_cost=cast(int, note.kdf_time_cost),
        memory_kib=cast(int, note.kdf_memory_kib),
        parallelism=cast(int, note.kdf_parallelism),
    )
    encrypted = crypto.EncryptedNote(kdf, note.wrapped_key, note.ciphertext)
    return ProtectedNote(
        id=note.id,
        slug=note.slug,
        encrypted=encrypted,
        has_encrypted_files=await db.run_sync(files_service.has_encrypted_files, note.id),
    )


async def open_protected(db: AsyncSession, protected: ProtectedNote) -> NoteWithFiles | None:
    """После верного пароля. None — пока считался Argon2, заметку удалили или сожгли."""
    note = await get_note_by_slug(db, protected.slug)
    if note is None or note.id != protected.id:
        return None
    return await _open(db, note)


async def delete_protected(db: AsyncSession, protected: ProtectedNote) -> bool:
    stmt = delete(Note).where(Note.id == protected.id).execution_options(synchronize_session=False)
    return cast(CursorResult[Any], await db.execute(stmt)).rowcount == 1


async def update_note(db: AsyncSession, note: Note, payload: NoteUpdate) -> Note:
    # exclude_unset: меняем только поля, которые клиент реально прислал
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(note, field, value)
    await db.flush()
    # updated_at выставляет БД (onupdate) — читаем явно, а не скрытой догрузкой
    await db.refresh(note)
    return note


async def delete_note(db: AsyncSession, note: Note) -> None:
    await db.delete(note)
    await db.flush()


async def delete_expired_notes(db: AsyncSession) -> int:
    # Один DELETE на все протухшие, без загрузки объектов в сессию
    stmt = (
        delete(Note)
        .where(Note.expires_at <= func.now())
        .execution_options(synchronize_session=False)
    )
    # execute() типизирован как общий Result, а rowcount есть только у CursorResult (DML)
    result = cast(CursorResult[Any], await db.execute(stmt))
    return result.rowcount
