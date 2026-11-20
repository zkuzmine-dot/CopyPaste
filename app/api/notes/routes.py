import logging
import time
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app.api.client import client_ip
from app.db.connection import DbSession
from app.db.models.note import Note
from app.schemas.file import LinkedFileRead
from app.schemas.note import NoteCreate, NotePassword, NoteRead, NoteUpdate
from app.security.attempts import AttemptsUnavailableError, PasswordAttempts
from app.services import files as files_service
from app.services import notes as notes_service
from app.settings import settings
from app.utils import crypto
from app.utils.qr import make_qr_svg

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notes", tags=["notes"])

PASSWORD_RESPONSES: dict[int | str, dict] = {
    403: {
        "description": "Wrong password; detail.attempts_left — попыток до заморозки. "
        "Если эта ошибка включила заморозку, Retry-After — через сколько секунд"
    },
    404: {"description": "Note not found"},
    409: {"description": "Note has no password"},
    429: {"description": "Too many wrong passwords; Retry-After — через сколько секунд"},
    503: {"description": "Password check is temporarily unavailable"},
}


def raise_not_found() -> NoReturn:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")


def get_note_or_404(db: DbSession, slug: str) -> Note:
    note = notes_service.get_note_by_slug(db, slug)
    if note is None:
        raise_not_found()
    return note


def _reject_if_protected(note: Note, instead: str) -> None:
    if note.has_password:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"Note is password-protected: use {instead}"
        )


def _links_expire_at(note: Note, ttl: int) -> int:
    expires = time.time() + ttl
    if note.expires_at is not None:
        expires = min(expires, note.expires_at.timestamp())
    return int(expires)


def _note_read(note: Note, files: list[LinkedFileRead], *, text: str | None) -> NoteRead:
    return NoteRead(
        slug=note.slug,
        text=text,
        created_at=note.created_at,
        updated_at=note.updated_at,
        expires_at=note.expires_at,
        burn_after_read=note.burn_after_read,
        has_password=note.has_password,
        files=files,
    )


def _present(db: Session, note: Note, *, hide_text: bool) -> NoteRead:
    # Пока одноразовую заметку не прочитали, а к заметке с паролем не ввели пароль,
    # ссылок на файлы нет: их выдают только POST /read и POST /unlock
    closed = note.burn_after_read or note.has_password
    links_expire = None if closed else _links_expire_at(note, settings.FILE_URL_TTL_SECONDS)
    files = [
        LinkedFileRead.build(f, links_expire=links_expire)
        for f in files_service.files_of_note(db, note.id)
    ]
    # Текст прячем в схеме, а не в ORM-объекте: note.text = None ушло бы в базу при коммите
    return _note_read(note, files, text=None if hide_text else note.text)


def _opened(result: notes_service.NoteWithFiles, *, text: str, links_ttl: int) -> NoteRead:
    note = result.note
    if not note.burn_after_read:
        links_expire = _links_expire_at(note, links_ttl)
        files = [LinkedFileRead.build(f, links_expire=links_expire) for f in result.files]
        return _note_read(note, files, text=text)
    files = []
    for stored in result.files:
        # keep_until выставлен в release_for_burn: ссылка живёт ровно столько же, сколько файл
        assert stored.keep_until is not None
        expires = int(stored.keep_until.timestamp())
        files.append(LinkedFileRead.build(stored, links_expire=expires, preview=False))
    return _note_read(note, files, text=text)


@router.post(
    "",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"description": "Invalid body or files are not available"}},
    description="file_ids — id из POST /api/files. Текст можно не присылать, если есть файлы. "
    "С password текст хранится зашифрованным, и в ответе его нет.",
)
def create_note(payload: NoteCreate, db: DbSession) -> NoteRead:
    try:
        note = notes_service.create_note(db, payload)
    except files_service.FilesNotAvailableError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Some files are unknown, already attached or uploaded too long ago",
        ) from None
    return _present(db, note, hide_text=note.has_password)


@router.get(
    "/{slug}",
    response_model=NoteRead,
    description="У одноразовой заметки и у заметки с паролем text = null и файлы без ссылок: "
    "их отдают POST /{slug}/read и POST /{slug}/unlock.",
)
def get_note(slug: str, db: DbSession) -> NoteRead:
    note = get_note_or_404(db, slug)
    return _present(db, note, hide_text=note.burn_after_read or note.has_password)


@router.post(
    "/{slug}/read",
    response_model=NoteRead,
    responses={409: {"description": "Note is password-protected"}},
    description="Возвращает текст и ссылки на файлы. Одноразовая заметка при этом удаляется, "
    "а каждый её файл можно скачать один раз за BURN_NOTE_FILES_WINDOW_SECONDS.",
)
def read_note(slug: str, db: DbSession) -> NoteRead:
    # Проверка до чтения: одноразовую заметку с паролем read сжёг бы, не спросив пароль
    _reject_if_protected(get_note_or_404(db, slug), "POST /{slug}/unlock")
    result = notes_service.read_note(db, slug)
    if result is None:
        raise_not_found()
    return _opened(result, text=result.note.text, links_ttl=settings.FILE_URL_TTL_SECONDS)


def _load_protected(request: Request, slug: str) -> notes_service.ProtectedNote:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        note = notes_service.get_note_by_slug(db, slug)
        if note is None:
            raise_not_found()
        protected = notes_service.protected_of(note)
    if protected is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Note has no password")
    return protected


def _freeze_headers(
    attempts: PasswordAttempts, ip: str, slug: str, attempts_left: int
) -> dict[str, str] | None:
    # Эта ошибка и включила заморозку: срок сообщаем сразу, а не на следующей попытке
    if attempts_left > 0:
        return None
    try:
        retry_after = attempts.retry_after(ip, slug)
    except AttemptsUnavailableError:
        # Что пароль неверный, уже известно — без срока ответ всё равно полезен
        return None
    return {"Retry-After": str(retry_after)} if retry_after is not None else None


def _check_password(
    request: Request, protected: notes_service.ProtectedNote, body: NotePassword
) -> str:
    """Текст заметки, если пароль верный. Argon2 считается без открытой сессии БД."""
    attempts = PasswordAttempts(request.app.state.redis)
    ip = client_ip(request)
    unavailable = HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, detail="Password check is temporarily unavailable"
    )
    try:
        # До Argon2: замороженный адрес не должен тратить память и CPU сервера
        retry_after = attempts.retry_after(ip, protected.slug)
    except AttemptsUnavailableError:
        raise unavailable from None
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many wrong passwords, try later",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        text = crypto.unlock_note(
            body.password.get_secret_value(), protected.encrypted, protected.slug
        )
    except crypto.DecryptionError:
        try:
            attempts_left = attempts.record_failure(ip, protected.slug)
        except AttemptsUnavailableError:
            raise unavailable from None
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"message": "Wrong password", "attempts_left": attempts_left},
            headers=_freeze_headers(attempts, ip, protected.slug, attempts_left),
        ) from None
    try:
        attempts.reset_note(protected.slug)
    except AttemptsUnavailableError:
        # Пароль верный — не повод отказывать; счётчик сам истечёт по окну
        logger.warning("Could not reset password attempts for note %s", protected.slug)
    return text


@router.post(
    "/{slug}/unlock",
    response_model=NoteRead,
    responses=PASSWORD_RESPONSES,
    description="Открыть заметку с паролем: текст и ссылки на файлы. Одноразовая заметка "
    "сгорает после верного пароля; неверный пароль её не трогает.",
)
def unlock_note(slug: str, body: NotePassword, request: Request, response: Response) -> NoteRead:
    protected = _load_protected(request, slug)
    text = _check_password(request, protected, body)
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db, db.begin():
        result = notes_service.open_protected(db, protected)
        if result is None:
            raise_not_found()
        # Расшифрованный текст не должен оседать в кэше браузера или прокси
        response.headers["Cache-Control"] = "no-store"
        return _opened(result, text=text, links_ttl=settings.PROTECTED_FILE_URL_TTL_SECONDS)


@router.post(
    "/{slug}/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PASSWORD_RESPONSES,
    description="Удалить заметку с паролем. Обычные заметки удаляются через DELETE.",
)
def delete_protected_note(slug: str, body: NotePassword, request: Request) -> None:
    protected = _load_protected(request, slug)
    _check_password(request, protected, body)
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db, db.begin():
        if not notes_service.delete_protected(db, protected):
            raise_not_found()


@router.get(
    "/{slug}/qr",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}, "description": "QR-код со ссылкой"}},
)
def note_qr(slug: str, request: Request, db: DbSession) -> Response:
    note = get_note_or_404(db, slug)
    url = str(request.url_for("note_page", slug=note.slug))
    return Response(make_qr_svg(url), media_type="image/svg+xml")


@router.put("/{slug}", response_model=NoteRead, responses={409: {"description": "Cannot edit"}})
def update_note(slug: str, payload: NoteUpdate, db: DbSession) -> NoteRead:
    note = get_note_or_404(db, slug)
    if note.burn_after_read:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Burn-after-read notes cannot be edited",
        )
    # Текст зашифрован паролем: изменить его, не зная пароля, нельзя
    _reject_if_protected(note, "a new note")
    return _present(db, notes_service.update_note(db, note, payload), hide_text=False)


@router.delete(
    "/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"description": "Note is password-protected"}},
)
def delete_note(slug: str, db: DbSession) -> None:
    note = get_note_or_404(db, slug)
    _reject_if_protected(note, "POST /{slug}/delete with the password")
    notes_service.delete_note(db, note)
