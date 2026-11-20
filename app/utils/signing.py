import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

from app.settings import settings


def _signature(public_id: str, expires: int, inline: bool, key_id: str | None = None) -> str:
    # Префикс "file:" — чтобы подпись ссылки нельзя было выдать за подпись чего-то другого,
    # если тот же секрет когда-нибудь начнёт подписывать что-то ещё
    message = f"file:{public_id}:{expires}:{int(inline)}"
    if key_id is not None:
        # Ссылка на зашифрованный файл: подменить в ней запись с ключом нельзя
        message += f":{key_id}"
    key = settings.FILES_SIGNING_SECRET.get_secret_value().encode()
    digest = hmac.new(key, message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def file_url(
    public_id: str, expires: int, *, inline: bool = False, key_id: str | None = None
) -> str:
    query: dict[str, str | int] = {
        "exp": expires,
        "sig": _signature(public_id, expires, inline, key_id),
    }
    if inline:
        query["inline"] = 1
    if key_id is not None:
        query["key_id"] = key_id
    return f"/api/files/{public_id}?{urlencode(query)}"


def received_url(public_id: str, expires: int) -> str:
    # Подпись — как у ссылки на скачивание, но без key_id: подтвердить получение может тот,
    # кто качал, а ключ для этого не нужен — подтверждение только удаляет файл
    query = {"exp": expires, "sig": _signature(public_id, expires, False)}
    return f"/api/files/{public_id}/received?{urlencode(query)}"


def is_valid_signature(
    public_id: str,
    expires: int,
    inline: bool,
    signature: str,
    *,
    key_id: str | None = None,
    now: float | None = None,
) -> bool:
    if expires < (time.time() if now is None else now):
        return False
    expected = _signature(public_id, expires, inline, key_id)
    # compare_digest: время сравнения не зависит от того, сколько символов совпало
    return hmac.compare_digest(signature.encode(), expected.encode())
