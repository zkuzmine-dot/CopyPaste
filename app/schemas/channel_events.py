from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from app.db.models.channel import ParticipantRole
from app.schemas.channel import MessageRead
from app.schemas.note import NoteText

# --- клиент → сервер ---


class AuthEvent(BaseModel):
    type: Literal["auth"]
    token: str


class SendEvent(BaseModel):
    type: Literal["send"]
    text: NoteText


class BecomeSenderEvent(BaseModel):
    type: Literal["become_sender"]


ClientEvent = Annotated[AuthEvent | SendEvent | BecomeSenderEvent, Field(discriminator="type")]
client_event_adapter: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)


# --- сервер → клиент ---


class StateEvent(BaseModel):
    type: Literal["state"] = "state"
    role: ParticipantRole
    peer_online: bool
    messages: list[MessageRead]


class MessageEvent(MessageRead):
    type: Literal["message"] = "message"


class RolesChangedEvent(BaseModel):
    type: Literal["roles_changed"] = "roles_changed"
    role: ParticipantRole


class PeerStatusEvent(BaseModel):
    type: Literal["peer_status"] = "peer_status"
    online: bool


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    detail: str


class ChannelClosedEvent(BaseModel):
    type: Literal["channel_closed"] = "channel_closed"
    reason: str
