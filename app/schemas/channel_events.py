from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from app.db.models.channel import ParticipantRole
from app.schemas.channel import (
    Base64Url,
    EncryptedText,
    KeyExchangeRead,
    MessageRead,
    PublicKey,
)

# --- клиент → сервер ---


class AuthEvent(BaseModel):
    type: Literal["auth"]
    token: str


class SendEvent(BaseModel):
    type: Literal["send"]
    payload: EncryptedText


class SendFileEvent(BaseModel):
    type: Literal["send_file"]
    # id из POST /api/channels/{slug}/files: сам файл идёт по HTTP, сюда — только ссылка на него
    file_id: str = Field(min_length=1, max_length=64)


class BecomeSenderEvent(BaseModel):
    type: Literal["become_sender"]


class PublicKeyEvent(BaseModel):
    type: Literal["public_key"]
    key: PublicKey


ClientEvent = Annotated[
    AuthEvent | SendEvent | SendFileEvent | BecomeSenderEvent | PublicKeyEvent,
    Field(discriminator="type"),
]
client_event_adapter: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)


# --- сервер → клиент ---


class StateEvent(KeyExchangeRead):
    type: Literal["state"] = "state"
    role: ParticipantRole
    peer_joined: bool
    peer_online: bool
    messages: list[MessageRead]


class PeerJoinedEvent(BaseModel):
    type: Literal["peer_joined"] = "peer_joined"
    key_commitment: Base64Url


class PeerKeyEvent(BaseModel):
    type: Literal["peer_key"] = "peer_key"
    key: Base64Url


class MessageEvent(MessageRead):
    type: Literal["message"] = "message"


class RolesChangedEvent(BaseModel):
    type: Literal["roles_changed"] = "roles_changed"
    role: ParticipantRole


class PeerStatusEvent(BaseModel):
    type: Literal["peer_status"] = "peer_status"
    online: bool


class FileDownloadedEvent(BaseModel):
    type: Literal["file_downloaded"] = "file_downloaded"
    message_id: int


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    detail: str


class ChannelClosedEvent(BaseModel):
    type: Literal["channel_closed"] = "channel_closed"
    reason: str
