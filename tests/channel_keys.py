"""Ключи и шифротекст для тестов канала.

Ключи — настоящие пары P-256, как у WebCrypto в браузере. Текст сервер не расшифровывает, поэтому
вместо шифротекста — метка между нулевыми «nonce» и «тегом»: нужной длины, и её видно в проверках.
"""

import base64
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx2 import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import Participant
from app.schemas.channel import MESSAGE_NONCE_BYTES, MESSAGE_TAG_BYTES
from app.services import channels as channels_service
from app.utils import channel_keys


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fake_payload(label: str) -> bytes:
    return bytes(MESSAGE_NONCE_BYTES) + label.encode() + bytes(MESSAGE_TAG_BYTES)


def fake_payload_b64(label: str) -> str:
    return b64url(fake_payload(label))


def text_of(message: dict) -> str:
    raw = base64.urlsafe_b64decode(message["payload"] + "==")
    return raw[MESSAGE_NONCE_BYTES:-MESSAGE_TAG_BYTES].decode()


@dataclass(frozen=True)
class DeviceKeys:
    public: bytes

    @property
    def commitment(self) -> bytes:
        return channel_keys.commitment(self.public)

    @property
    def public_b64(self) -> str:
        return b64url(self.public)

    @property
    def commitment_b64(self) -> str:
        return b64url(self.commitment)


def device_keys() -> DeviceKeys:
    private = ec.generate_private_key(ec.SECP256R1())
    raw = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return DeviceKeys(raw)


async def join_by_http(client: AsyncClient, slug: str, keys: DeviceKeys | None = None) -> Response:
    keys = keys or device_keys()
    return await client.post(
        f"/api/channels/{slug}/join", json={"key_commitment": keys.commitment_b64}
    )


async def exchange_keys(db: AsyncSession, joiner: Participant, joiner_keys: DeviceKeys) -> None:
    """Обмен ключами, как его провели бы браузеры: сначала создатель, потом подключившийся."""
    stmt = select(Participant).where(
        Participant.channel_id == joiner.channel_id, Participant.id != joiner.id
    )
    creator = (await db.execute(stmt)).scalar_one()
    await channels_service.submit_public_key(db, creator, device_keys().public)
    await channels_service.submit_public_key(db, joiner, joiner_keys.public)


async def join_ready(db: AsyncSession, slug: str) -> tuple[Participant, str]:
    """Второе устройство вошло, и ключи уже обменяны: в канал можно писать."""
    keys = device_keys()
    joiner, token = await channels_service.join_channel(db, slug, keys.commitment)
    await exchange_keys(db, joiner, keys)
    return joiner, token


async def exchange_keys_by_token(
    db: AsyncSession, slug: str, joiner_token: str, joiner_keys: DeviceKeys
) -> None:
    channel = await channels_service.get_active_channel(db, slug)
    assert channel is not None
    joiner = await channels_service.get_participant(db, channel, joiner_token)
    assert joiner is not None
    await exchange_keys(db, joiner, joiner_keys)
