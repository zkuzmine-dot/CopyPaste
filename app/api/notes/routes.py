import time
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.db.connection import DbSession
from app.db.models.file import StoredFile
from app.db.models.note import Note
from app.schemas.file import FileRead
from app.schemas.note import NoteCreate, NoteFileRead, NoteRead, NoteUpdate
from app.services import files as files_service
from app.services import notes as notes_service
from app.settings import settings
from app.utils import signing
from app.utils.qr import make_qr_svg

router = APIRouter(prefix="/api/notes", tags=["notes"])


def raise_not_found() -> NoReturn:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")


def get_note_or_404(db: DbSession, slug: str) -> Note:
    note = notes_service.get_note_by_slug(db, slug)
    if note is None:
        raise_not_found()
    return note


def _links_expire_at(note: Note) -> int:
    expires = time.time() + settings.FILE_URL_TTL_SECONDS
    if note.expires_at is not None:
        expires = min(expires, note.expires_at.timestamp())
    return int(expires)


def _file_read(
    stored: StoredFile, *, links_expire: int | None, preview: bool = True
) -> NoteFileRead:
    info = FileRead.from_stored(stored).model_dump()
    if links_expire is None:
        return NoteFileRead(**info, download_url=None, preview_url=None)
    preview_url = None
    if preview and info["is_image"]:
        preview_url = signing.file_url(stored.public_id, links_expire, inline=True)
    return NoteFileRead(
        **info,
        download_url=signing.file_url(stored.public_id, links_expire),
        preview_url=preview_url,
    )


def _note_read(note: Note, files: list[NoteFileRead], *, hide_text: bool = False) -> NoteRead:
    # Текст прячем в схеме, а не в ORM-объекте: note.text = None ушло бы в базу при коммите
    return NoteRead(
        slug=note.slug,
        text=None if hide_text else note.text,
        created_at=note.created_at,
        updated_at=note.updated_at,
        expires_at=note.expires_at,
        burn_after_read=note.burn_after_read,
        files=files,
    )


def _present(db: Session, note: Note) -> NoteRead:
    # У одноразовой заметки ссылок нет, пока её не прочитали: их выдаёт только POST /read
    links_expire = None if note.burn_after_read else _links_expire_at(note)
    files = files_service.files_of_note(db, note.id)
    return _note_read(note, [_file_read(f, links_expire=links_expire) for f in files])


@router.post(
    "",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"description": "Invalid body or files are not available"}},
    description="file_ids — id из POST /api/files. Текст можно не присылать, если есть файлы.",
)
def create_note(payload: NoteCreate, db: DbSession) -> NoteRead:
    try:
        note = notes_service.create_note(db, payload)
    except files_service.FilesNotAvailableError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Some files are unknown, already attached or uploaded too long ago",
        ) from None
    return _present(db, note)


@router.get(
    "/{slug}",
    response_model=NoteRead,
    description="У одноразовой заметки text = null и файлы без ссылок: "
    "прочитать её можно через POST /{slug}/read.",
)
def get_note(slug: str, db: DbSession) -> NoteRead:
    note = get_note_or_404(db, slug)
    result = _present(db, note)
    if note.burn_after_read:
        result.text = None
    return result


@router.post(
    "/{slug}/read",
    response_model=NoteRead,
    description="Возвращает текст и ссылки на файлы. Одноразовая заметка при этом удаляется, "
    "а каждый её файл можно скачать один раз за BURN_NOTE_FILES_WINDOW_SECONDS.",
)
def read_note(slug: str, db: DbSession) -> NoteRead:
    result = notes_service.read_note(db, slug)
    if result is None:
        raise_not_found()
    note = result.note
    if not note.burn_after_read:
        links_expire = _links_expire_at(note)
        return _note_read(note, [_file_read(f, links_expire=links_expire) for f in result.files])

    files = []
    for stored in result.files:
        # keep_until выставлен в release_for_burn: ссылка живёт ровно столько же, сколько файл
        assert stored.keep_until is not None
        expires = int(stored.keep_until.timestamp())
        files.append(_file_read(stored, links_expire=expires, preview=False))
    return _note_read(note, files)


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
def update_note(slug: str, payload: NoteUpdate, db: DbSession) -> NoteRead:
    note = get_note_or_404(db, slug)
    if note.burn_after_read:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Burn-after-read notes cannot be edited",
        )
    return _present(db, notes_service.update_note(db, note, payload))


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(slug: str, db: DbSession) -> None:
    note = get_note_or_404(db, slug)
    notes_service.delete_note(db, note)
