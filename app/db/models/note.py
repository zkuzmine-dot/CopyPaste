from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, LargeBinary, String, Text, false, func
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
        # Заметка не может быть зашифрована «наполовину»: либо все шесть полей, либо ни одного
        CheckConstraint(
            "num_nulls(kdf_salt, kdf_time_cost, kdf_memory_kib, kdf_parallelism, "
            "wrapped_key, ciphertext) IN (0, 6)",
            name="ck_notes_encryption_complete",
        ),
        # Открытый текст не лежит рядом с шифротекстом
        CheckConstraint("wrapped_key IS NULL OR text = ''", name="ck_notes_encrypted_text_empty"),
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
    burn_after_read: Mapped[bool] = mapped_column(default=False, server_default=false())
    kdf_salt: Mapped[bytes | None] = mapped_column(LargeBinary)
    kdf_time_cost: Mapped[int | None]
    kdf_memory_kib: Mapped[int | None]
    kdf_parallelism: Mapped[int | None]
    wrapped_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)

    @property
    def has_password(self) -> bool:
        return self.wrapped_key is not None
