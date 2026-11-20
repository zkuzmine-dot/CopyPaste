"""add password to notes

Revision ID: a7c3e9f1d5b2
Revises: f2b9d6c4e8a1
Create Date: 2026-11-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f1d5b2"
down_revision: str | Sequence[str] | None = "f2b9d6c4e8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("notes", sa.Column("kdf_salt", sa.LargeBinary(), nullable=True))
    op.add_column("notes", sa.Column("kdf_time_cost", sa.Integer(), nullable=True))
    op.add_column("notes", sa.Column("kdf_memory_kib", sa.Integer(), nullable=True))
    op.add_column("notes", sa.Column("kdf_parallelism", sa.Integer(), nullable=True))
    op.add_column("notes", sa.Column("wrapped_key", sa.LargeBinary(), nullable=True))
    op.add_column("notes", sa.Column("ciphertext", sa.LargeBinary(), nullable=True))
    op.create_check_constraint(
        "ck_notes_encryption_complete",
        "notes",
        "num_nulls(kdf_salt, kdf_time_cost, kdf_memory_kib, kdf_parallelism, "
        "wrapped_key, ciphertext) IN (0, 6)",
    )
    op.create_check_constraint(
        "ck_notes_encrypted_text_empty", "notes", "wrapped_key IS NULL OR text = ''"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Без колонок шифрования такие заметки не открыть, а открытого текста у них нет
    op.execute("DELETE FROM notes WHERE wrapped_key IS NOT NULL")
    op.drop_constraint("ck_notes_encrypted_text_empty", "notes", type_="check")
    op.drop_constraint("ck_notes_encryption_complete", "notes", type_="check")
    op.drop_column("notes", "ciphertext")
    op.drop_column("notes", "wrapped_key")
    op.drop_column("notes", "kdf_parallelism")
    op.drop_column("notes", "kdf_memory_kib")
    op.drop_column("notes", "kdf_time_cost")
    op.drop_column("notes", "kdf_salt")
