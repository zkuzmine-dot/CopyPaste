"""add encryption to stored files

Revision ID: b6e1d94c2f7a
Revises: a7c3e9f1d5b2
Create Date: 2026-11-20 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6e1d94c2f7a"
down_revision: str | Sequence[str] | None = "a7c3e9f1d5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("stored_files", sa.Column("encryption_nonce", sa.LargeBinary(), nullable=True))
    op.add_column("stored_files", sa.Column("chunk_size", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_stored_files_encryption_complete",
        "stored_files",
        "num_nulls(encryption_nonce, chunk_size) IN (0, 2)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Без колонок такие файлы отдавались бы шифротекстом. Байты на диске потом сотрёт сверка
    op.execute("DELETE FROM stored_files WHERE encryption_nonce IS NOT NULL")
    op.drop_constraint("ck_stored_files_encryption_complete", "stored_files", type_="check")
    op.drop_column("stored_files", "chunk_size")
    op.drop_column("stored_files", "encryption_nonce")
