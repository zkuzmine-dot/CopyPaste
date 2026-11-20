from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select
from sqlalchemy.orm import Session

from app.db.models.file import StoredFile
from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteUpdate
from app.services import files as files_service
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


def create_note(db: Session, payload: NoteCreate) -> Note:
    """FilesNotAvailableError, если часть файлов привязать нельзя: транзакцию нужно откатить."""
    note = Note(
        slug=generate_slug(),
        text=payload.text,
        expires_at=payload.expires_at,
        burn_after_read=payload.burn_after_read,
    )
    if payload.password is not None:
        # До первого запроса к БД: соединение берётся лениво и на время Argon2 не занято
        encrypted = crypto.encrypt_note(payload.password.get_secret_value(), note.text, note.slug)
        note.text = ""
        note.kdf_salt = encrypted.kdf.salt
        note.kdf_time_cost = encrypted.kdf.time_cost
        note.kdf_memory_kib = encrypted.kdf.memory_kib
        note.kdf_parallelism = encrypted.kdf.parallelism
        note.wrapped_key = encrypted.wrapped_key
        note.ciphertext = encrypted.ciphertext
    db.add(note)
    # id и created_at приходят из INSERT ... RETURNING, refresh не нужен
    db.flush()
    files_service.attach_to_note(db, note.id, payload.file_ids)
    return note


def get_note_by_slug(db: Session, slug: str) -> Note | None:
    # Протухшие заметки считаем несуществующими
    stmt = select(Note).where(
        Note.slug == slug,
        or_(Note.expires_at.is_(None), Note.expires_at > func.now()),
    )
    return db.execute(stmt).scalar_one_or_none()


def _lock_note(db: Session, note_id: int) -> bool:
    stmt = select(Note.id).where(Note.id == note_id).with_for_update()
    return db.execute(stmt).first() is not None


def _open(db: Session, note: Note) -> NoteWithFiles | None:
    if not note.burn_after_read:
        return NoteWithFiles(note, files_service.files_of_note(db, note.id))
    # Сначала блокируем заметку, потом файлы — в том же порядке, что и DELETE заметки
    # (сама строка, потом SET NULL у файлов). Иначе встречные блокировки дали бы deadlock.
    # Второй одновременный читатель дождётся блокировки и строки уже не найдёт
    if not _lock_note(db, note.id):
        return None
    files = files_service.release_for_burn(db, note.id)
    db.execute(delete(Note).where(Note.id == note.id).execution_options(synchronize_session=False))
    return NoteWithFiles(note, files)


def read_note(db: Session, slug: str) -> NoteWithFiles | None:
    note = get_note_by_slug(db, slug)
    if note is None:
        return None
    return _open(db, note)


def protected_of(note: Note) -> ProtectedNote | None:
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
    return ProtectedNote(id=note.id, slug=note.slug, encrypted=encrypted)


def open_protected(db: Session, protected: ProtectedNote) -> NoteWithFiles | None:
    """После верного пароля. None — пока считался Argon2, заметку удалили или сожгли."""
    note = get_note_by_slug(db, protected.slug)
    if note is None or note.id != protected.id:
        return None
    return _open(db, note)


def delete_protected(db: Session, protected: ProtectedNote) -> bool:
    stmt = delete(Note).where(Note.id == protected.id).execution_options(synchronize_session=False)
    return cast(CursorResult[Any], db.execute(stmt)).rowcount == 1


def update_note(db: Session, note: Note, payload: NoteUpdate) -> Note:
    # exclude_unset: меняем только поля, которые клиент реально прислал
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(note, field, value)
    db.flush()
    db.refresh(note)
    return note


def delete_note(db: Session, note: Note) -> None:
    db.delete(note)
    db.flush()


def delete_expired_notes(db: Session) -> int:
    # Один DELETE на все протухшие, без загрузки объектов в сессию
    stmt = (
        delete(Note)
        .where(Note.expires_at <= func.now())
        .execution_options(synchronize_session=False)
    )
    # execute() типизирован как общий Result, а rowcount есть только у CursorResult (DML)
    result = cast(CursorResult[Any], db.execute(stmt))
    return result.rowcount
