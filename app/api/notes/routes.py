from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.db.connection import DbSession
from app.db.models.note import Note
from app.schemas.note import NoteCreate, NoteRead, NoteUpdate
from app.services import notes as notes_service
from app.utils.qr import make_qr_svg

router = APIRouter(prefix="/api/notes", tags=["notes"])


def raise_not_found() -> NoReturn:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")


def get_note_or_404(db: DbSession, slug: str) -> Note:
    note = notes_service.get_note_by_slug(db, slug)
    if note is None:
        raise_not_found()
    return note


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
def create_note(payload: NoteCreate, db: DbSession) -> Note:
    return notes_service.create_note(db, payload)


@router.get(
    "/{slug}",
    response_model=NoteRead,
    description="У одноразовой заметки text = null: прочитать её можно через POST /{slug}/read.",
)
def get_note(slug: str, db: DbSession) -> NoteRead:
    note = get_note_or_404(db, slug)
    result = NoteRead.model_validate(note)
    if note.burn_after_read:
        # Прячем в схеме, а не в ORM-объекте: note.text = None ушло бы в базу при коммите
        result.text = None
    return result


@router.post(
    "/{slug}/read",
    response_model=NoteRead,
    description="Возвращает текст. Одноразовая заметка при этом удаляется.",
)
def read_note(slug: str, db: DbSession) -> Note:
    note = notes_service.read_note(db, slug)
    if note is None:
        raise_not_found()
    return note


@router.get(
    "/{slug}/qr",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}, "description": "QR-код со ссылкой"}},
)
def note_qr(slug: str, request: Request, db: DbSession) -> Response:
    note = get_note_or_404(db, slug)
    url = str(request.url_for("note_page", slug=note.slug))
    return Response(make_qr_svg(url), media_type="image/svg+xml")


@router.put("/{slug}", response_model=NoteRead)
def update_note(slug: str, payload: NoteUpdate, db: DbSession) -> Note:
    note = get_note_or_404(db, slug)
    if note.burn_after_read:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Burn-after-read notes cannot be edited",
        )
    return notes_service.update_note(db, note, payload)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(slug: str, db: DbSession) -> None:
    note = get_note_or_404(db, slug)
    notes_service.delete_note(db, note)
