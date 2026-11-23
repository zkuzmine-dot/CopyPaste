import base64
import binascii
import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer, WithJsonSchema

from app.db.models.channel import MessageKind, ParticipantRole
from app.schemas.file import LinkedFileRead
from app.utils import channel_keys

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


_AS_BASE64URL = PlainSerializer(_to_base64url, return_type=str)
# Входящие значения проверяются, исходящие — из базы, их только кодируем
PublicKey = Annotated[bytes, BeforeValidator(_public_key), _AS_BASE64URL, _BASE64URL_SCHEMA]
KeyCommitment = Annotated[bytes, BeforeValidator(_commitment), _AS_BASE64URL, _BASE64URL_SCHEMA]
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
    text: str | None
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
