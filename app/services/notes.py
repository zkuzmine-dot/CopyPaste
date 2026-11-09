from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select
from sqlalchemy.orm import Session

from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteUpdate


def create_note(db: Session, payload: NoteCreate) -> Note:
    note = Note(text=payload.text, expires_at=payload.expires_at)
    db.add(note)
    # id и created_at приходят из INSERT ... RETURNING, refresh не нужен
    db.flush()
    return note


def get_note_by_slug(db: Session, slug: str) -> Note | None:
    # Протухшие заметки считаем несуществующими
    stmt = select(Note).where(
        Note.slug == slug,
        or_(Note.expires_at.is_(None), Note.expires_at > func.now()),
    )
    return db.execute(stmt).scalar_one_or_none()


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
