from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.slug import SLUG_LENGTH, generate_slug


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        Index(
            "ix_notes_expires_at", "expires_at", postgresql_where=sql_text("expires_at IS NOT NULL")
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    slug: Mapped[str] = mapped_column(
        String(SLUG_LENGTH), unique=True, index=True, default=generate_slug
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
