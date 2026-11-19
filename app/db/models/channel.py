from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils.slug import SLUG_LENGTH, generate_slug


class ParticipantRole(StrEnum):
    SENDER = "sender"
    RECEIVER = "receiver"


class MessageKind(StrEnum):
    TEXT = "text"
    FILE = "file"


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(
        String(SLUG_LENGTH), unique=True, index=True, default=generate_slug
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        CheckConstraint("role IN ('sender', 'receiver')", name="ck_participants_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    channel: Mapped[Channel] = relationship()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_channel_id_created_at", "channel_id", "created_at"),
        CheckConstraint("kind IN ('text', 'file')", name="ck_messages_kind"),
        # Текст есть ровно у текстовых сообщений; файл лежит в stored_files.message_id
        CheckConstraint(
            "(kind = 'text') = (text IS NOT NULL)", name="ck_messages_text_matches_kind"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    sender_id: Mapped[int] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(
        String(8), default=MessageKind.TEXT, server_default=MessageKind.TEXT.value
    )
    text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
