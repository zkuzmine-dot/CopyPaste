from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.channel import MessageKind, ParticipantRole
from app.schemas.file import LinkedFileRead


class ChannelCreated(BaseModel):
    slug: str
    token: str
    role: ParticipantRole


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


class ChannelState(BaseModel):
    slug: str
    role: ParticipantRole
    peer_joined: bool
    messages: list[MessageRead]
