"""Файлы заметок с паролем: на диске лежат зашифрованными, расшифровываются при отдаче."""

import hashlib
import os
from collections.abc import Iterator, Mapping
from typing import BinaryIO

from starlette.responses import Response, StreamingResponse

from app.storage.base import FileStorage, new_storage_key
from app.utils import crypto
from app.utils.http_range import RangeNotSatisfiableError, parse_range


def encrypt_upload(storage: FileStorage, source_key: str, cipher: crypto.FileCipher) -> str:
    """Пишет зашифрованную копию под новым ключом и возвращает его.

    Исходник не трогаем: БД ещё указывает на него, пока транзакция не закоммичена.
    """
    target = new_storage_key()
    with storage.open(source_key) as source:
        storage.write(target, crypto.encrypt_file(cipher, source))
    return target


def _plain_bytes(
    file: BinaryIO, cipher: crypto.FileCipher, stored_size: int, start: int, end: int
) -> Iterator[bytes]:
    try:
        if end < start:
            return
        first, last = start // cipher.chunk_size, end // cipher.chunk_size
        chunks = crypto.decrypt_file(cipher, file, stored_size, first=first)
        for index, plain in enumerate(chunks, start=first):
            offset = index * cipher.chunk_size
            yield plain[max(start - offset, 0) : end - offset + 1]
            if index == last:
                return
    finally:
        file.close()


def encrypted_download(
    storage: FileStorage,
    key: str,
    cipher: crypto.FileCipher,
    *,
    media_type: str,
    headers: Mapping[str, str],
    range_header: str | None,
    if_range: str | None,
) -> Response:
    """Ответ с расшифрованным файлом. FileNotFoundError, если байтов нет.

    Подменённый или обрезанный кусок даёт DecryptionError, а не мусор: если ошибка
    случится посреди отдачи, соединение оборвётся раньше Content-Length.
    """
    file = storage.open(key)
    try:
        stored_size = os.fstat(file.fileno()).st_size
        size = crypto.plain_size(stored_size, cipher)
    except BaseException:
        file.close()
        raise
    # Содержимое по этому id не меняется — достаточно метки от nonce файла
    etag = f'"{hashlib.sha256(cipher.nonce).hexdigest()[:32]}"'
    if if_range is not None and if_range != etag:
        # У клиента начало другого файла — склеивать с ним нельзя, отдаём целиком
        range_header = None
    try:
        span = parse_range(range_header, size)
    except RangeNotSatisfiableError:
        file.close()
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    start, end = span if span is not None else (0, size - 1)
    response_headers = {
        **headers,
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "ETag": etag,
    }
    if span is not None:
        response_headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        _plain_bytes(file, cipher, stored_size, start, end),
        status_code=206 if span is not None else 200,
        media_type=media_type,
        headers=response_headers,
    )
