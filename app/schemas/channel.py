import base64
import binascii
import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer, WithJsonSchema

from app.db.models.channel import MessageKind, ParticipantRole
from app.schemas.file import LinkedFileRead
from app.schemas.note import NOTE_TEXT_MAX_LENGTH
from app.utils import channel_keys

# Текст шифрует браузер (AES-GCM): сервер видит nonce, шифротекст и тег. Поле ввода ограничивает
# текст NOTE_TEXT_MAX_LENGTH знаками UTF-16, а в UTF-8 такой знак занимает не больше 3 байт
MESSAGE_NONCE_BYTES = 12
MESSAGE_TAG_BYTES = 16
MESSAGE_PAYLOAD_MIN_BYTES = MESSAGE_NONCE_BYTES + 1 + MESSAGE_TAG_BYTES
MESSAGE_PAYLOAD_MAX_BYTES = MESSAGE_NONCE_BYTES + 3 * NOTE_TEXT_MAX_LENGTH + MESSAGE_TAG_BYTES
# Описание файла (имя, размер, nonce) — JSON, зашифрованный так же. Имя браузер обрезает до
# 255 знаков: даже если каждый экранируется как \uXXXX, описание укладывается в 2 КБ
FILE_META_MAX_BYTES = 2048
FILE_META_PAYLOAD_MAX_BYTES = MESSAGE_NONCE_BYTES + FILE_META_MAX_BYTES + MESSAGE_TAG_BYTES
# Сам файл браузер шифрует кусками, к каждому добавляется тег
CHANNEL_FILE_CHUNK_BYTES = 65_536

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_BASE64URL_SCHEMA = WithJsonSchema(
    {"type": "string", "pattern": "^[A-Za-z0-9_-]+$", "description": "base64url без «=»"}
)


def _from_base64url(value: object) -> bytes:
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        raise ValueError("must be base64url without padding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except binascii.Error:
        raise ValueError("must be base64url without padding") from None


def _to_base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _public_key(value: object) -> bytes:
    raw = _from_base64url(value)
    if not channel_keys.is_public_key(raw):
        raise ValueError("must be an uncompressed P-256 public key")
    return raw


def _commitment(value: object) -> bytes:
    raw = _from_base64url(value)
    if len(raw) != channel_keys.COMMITMENT_BYTES:
        raise ValueError("must be a SHA-256 digest")
    return raw


def _encrypted(max_bytes: int) -> Callable[[object], bytes]:
    def validate(value: object) -> bytes:
        raw = _from_base64url(value)
        if not MESSAGE_PAYLOAD_MIN_BYTES <= len(raw) <= max_bytes:
            raise ValueError(
                f"must be {MESSAGE_PAYLOAD_MIN_BYTES}-{max_bytes} bytes: nonce, ciphertext and tag"
            )
        return raw

    return validate


# Только в JSON: внутри приложения (model_dump) байты остаются байтами
_AS_BASE64URL = PlainSerializer(_to_base64url, return_type=str, when_used="json")
# Входящие значения проверяются, исходящие — из базы, их только кодируем
PublicKey = Annotated[bytes, BeforeValidator(_public_key), _AS_BASE64URL, _BASE64URL_SCHEMA]
KeyCommitment = Annotated[bytes, BeforeValidator(_commitment), _AS_BASE64URL, _BASE64URL_SCHEMA]
EncryptedText = Annotated[
    bytes, BeforeValidator(_encrypted(MESSAGE_PAYLOAD_MAX_BYTES)), _AS_BASE64URL, _BASE64URL_SCHEMA
]
EncryptedFileMeta = Annotated[
    bytes,
    BeforeValidator(_encrypted(FILE_META_PAYLOAD_MAX_BYTES)),
    _AS_BASE64URL,
    _BASE64URL_SCHEMA,
]
Base64Url = Annotated[bytes, _AS_BASE64URL, _BASE64URL_SCHEMA]


class ChannelCreated(BaseModel):
    slug: str
    token: str
    role: ParticipantRole


class ChannelJoin(BaseModel):
    key_commitment: KeyCommitment


class ChannelJoined(BaseModel):
    slug: str
    token: str
    role: ParticipantRole


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: MessageKind
    # Зашифрованный текст или описание файла
    payload: Base64Url
    # Ссылки в file у каждого участника свои, поэтому собирается не из атрибутов модели
    file: LinkedFileRead | None = None
    created_at: datetime
    expires_at: datetime


class KeyExchangeRead(BaseModel):
    # Принял ли сервер мой ключ: после перезагрузки страница не отправит его второй раз
    key_sent: bool
    # Отпечаток есть только у подключившегося, поэтому его видит только создатель
    peer_key_commitment: Base64Url | None
    peer_public_key: Base64Url | None


class ChannelState(KeyExchangeRead):
    slug: str
    role: ParticipantRole
    peer_joined: bool
    messages: list[MessageRead]
