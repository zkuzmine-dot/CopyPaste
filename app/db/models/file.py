import secrets
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    false,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FileOrigin(StrEnum):
    NOTE = "note"
    CHANNEL = "channel"


def generate_public_id() -> str:
    return secrets.token_urlsafe(16)


class StoredFile(Base):
    __tablename__ = "stored_files"
    __table_args__ = (
        CheckConstraint("origin IN ('note', 'channel')", name="ck_stored_files_origin"),
        CheckConstraint(
            "note_id IS NULL OR message_id IS NULL", name="ck_stored_files_single_owner"
        ),
        Index(
            "ix_stored_files_orphan_keep_until",
            "keep_until",
            postgresql_where=sql_text("note_id IS NULL AND message_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(22), unique=True, index=True, default=generate_public_id
    )
    storage_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    origin: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # SET NULL, а не CASCADE: строка переживает владельца, чтобы сборщик знал, какие байты стереть
    note_id: Mapped[int | None] = mapped_column(
        ForeignKey("notes.id", ondelete="SET NULL"), index=True
    )
    position: Mapped[int | None] = mapped_column(SmallInteger)
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), unique=True, index=True
    )
    uploaded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), index=True
    )
    keep_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_after_download: Mapped[bool] = mapped_column(default=False, server_default=false())
