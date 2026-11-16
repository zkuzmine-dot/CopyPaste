from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.channel import ParticipantRole


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
    text: str
    created_at: datetime
    expires_at: datetime


class ChannelState(BaseModel):
    slug: str
    role: ParticipantRole
    peer_joined: bool
    messages: list[MessageRead]
