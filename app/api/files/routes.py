from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.db.connection import run_db
from app.db.models.file import FileOrigin
from app.realtime.rate_limit import KeyedRateLimiter
from app.schemas.file import FileRead
from app.services import files as files_service
from app.settings import settings
from app.storage.base import FileStorage, FileTooLargeError, new_storage_key
from app.storage.responses import OnComplete
from app.utils import signing
from app.utils.file_types import (
    SNIFF_BYTES,
    clean_filename,
    content_disposition,
    detect_content_type,
    is_previewable,
)

router = APIRouter(prefix="/api/files", tags=["files"])

# Файл может оказаться HTML или SVG. Даже если браузер откроет его, он не станет угадывать
# тип (nosniff) и не выполнит скрипты (sandbox)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "private, no-store",
}


def create_upload_rate_limiter() -> KeyedRateLimiter:
    return KeyedRateLimiter(
        settings.UPLOAD_RATE_LIMIT_PER_MINUTE / 60, settings.UPLOAD_RATE_LIMIT_BURST
    )


def _too_large() -> HTTPException:
    return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="File is too large")


def _empty() -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail="File is empty")


def _gone() -> HTTPException:
    return HTTPException(status.HTTP_410_GONE, detail="File is no longer available")


async def receive_upload(
    request: Request, name: str, *, origin: FileOrigin, uploaded_by_id: int | None = None
) -> FileRead:
    """Читает тело запроса потоком в хранилище и записывает файл в БД пока без владельца.

    Сессию БД во время чтения не держим: медленные загрузки иначе заняли бы весь пул.
    """
    limiter: KeyedRateLimiter = request.app.state.upload_rate_limiter
    if not limiter.allow(request.client.host if request.client else "unknown"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many uploads")

    # Content-Length проверяем до чтения, но не полагаемся на него: его может не быть
    header = request.headers.get("content-length")
    declared_size = int(header) if header is not None else None
    if declared_size is not None and declared_size > settings.FILE_MAX_SIZE_BYTES:
        raise _too_large()
    if declared_size == 0:
        raise _empty()

    factory: sessionmaker[Session] = request.app.state.session_factory
    storage: FileStorage = request.app.state.file_storage
    used = await run_db(factory, files_service.total_size)
    if used + (declared_size or 0) > settings.FILES_MAX_TOTAL_BYTES:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, detail="Storage is full")

    head = bytearray()

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            if len(head) < SNIFF_BYTES:
                head.extend(chunk[: SNIFF_BYTES - len(head)])
            yield chunk

    key = new_storage_key()
    try:
        size = await storage.save(key, chunks(), settings.FILE_MAX_SIZE_BYTES)
    except FileTooLargeError:
        raise _too_large() from None
    except ClientDisconnect:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Upload interrupted") from None

    def record(db: Session) -> FileRead:
        stored = files_service.create_upload(
            db,
            storage_key=key,
            name=clean_filename(name),
            content_type=detect_content_type(bytes(head)),
            size_bytes=size,
            origin=origin,
            uploaded_by_id=uploaded_by_id,
        )
        return FileRead.from_stored(stored)

    try:
        if size == 0:
            raise _empty()
        return await run_db(factory, record)
    except BaseException:
        # Без строки в БД эти байты никто не найдёт — стираем сразу
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(storage.delete, key)
        raise


@router.post(
    "",
    response_model=FileRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Empty file"},
        413: {"description": "File is too large"},
        429: {"description": "Too many uploads"},
        507: {"description": "Storage is full"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
    description="Тело запроса — сами байты файла. Файл ждёт привязки к заметке час.",
)
async def upload_file(
    request: Request, name: Annotated[str, Query(min_length=1, max_length=1024)]
) -> FileRead:
    return await receive_upload(request, name, origin=FileOrigin.NOTE)


@dataclass(frozen=True)
class _Download:
    id: int
    storage_key: str
    name: str
    content_type: str
    delete_after_download: bool


def _find_download(db: Session, public_id: str) -> _Download | None:
    stored = files_service.get_available_file(db, public_id)
    if stored is None:
        return None
    return _Download(
        id=stored.id,
        storage_key=stored.storage_key,
        name=stored.name,
        content_type=stored.content_type,
        delete_after_download=stored.delete_after_download,
    )


@router.get(
    "/{public_id}",
    response_class=Response,
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "Файл"},
        400: {"description": "Preview is only available for images"},
        403: {"description": "Invalid or expired link"},
        410: {"description": "File is no longer available"},
    },
)
async def download_file(
    request: Request, public_id: str, exp: int, sig: str, inline: bool = False
) -> Response:
    # Подпись проверяется без БД: по чужой или старой ссылке до базы дело не доходит
    if not signing.is_valid_signature(public_id, exp, inline, sig):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid or expired link")

    factory: sessionmaker[Session] = request.app.state.session_factory
    storage: FileStorage = request.app.state.file_storage
    found = await run_db(factory, lambda db: _find_download(db, public_id))
    # Подпись верная — файл точно был, значит, его уже скачали или срок вышел
    if found is None:
        raise _gone()
    if inline and not is_previewable(found.content_type):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Preview is only available for images"
        )

    on_complete: OnComplete | None = None
    if found.delete_after_download and not inline:
        file_id = found.id

        async def forget() -> None:
            key = await run_db(factory, lambda db: files_service.delete_file(db, file_id))
            if key is not None:
                await run_in_threadpool(storage.delete, key)

        on_complete = forget

    headers = {
        **SECURITY_HEADERS,
        "Content-Disposition": content_disposition(found.name, inline=inline),
    }
    try:
        return storage.download(
            found.storage_key,
            media_type=found.content_type,
            headers=headers,
            on_complete=on_complete,
        )
    except FileNotFoundError:
        raise _gone() from None
