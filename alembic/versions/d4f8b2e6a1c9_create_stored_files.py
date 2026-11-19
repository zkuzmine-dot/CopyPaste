"""create stored_files

Revision ID: d4f8b2e6a1c9
Revises: c5d2a7e93f18
Create Date: 2026-11-17 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f8b2e6a1c9"
down_revision: str | Sequence[str] | None = "c5d2a7e93f18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "stored_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=22), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("keep_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "delete_after_download", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.CheckConstraint("origin IN ('note', 'channel')", name="ck_stored_files_origin"),
        sa.CheckConstraint(
            "note_id IS NULL OR message_id IS NULL", name="ck_stored_files_single_owner"
        ),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["participants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stored_files_public_id"), "stored_files", ["public_id"], unique=True)
    op.create_index(
        op.f("ix_stored_files_storage_key"), "stored_files", ["storage_key"], unique=True
    )
    op.create_index(op.f("ix_stored_files_note_id"), "stored_files", ["note_id"])
    op.create_index(
        op.f("ix_stored_files_message_id"), "stored_files", ["message_id"], unique=True
    )
    op.create_index(op.f("ix_stored_files_uploaded_by_id"), "stored_files", ["uploaded_by_id"])
    op.create_index(
        "ix_stored_files_orphan_keep_until",
        "stored_files",
        ["keep_until"],
        postgresql_where=sa.text("note_id IS NULL AND message_id IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_stored_files_orphan_keep_until",
        table_name="stored_files",
        postgresql_where=sa.text("note_id IS NULL AND message_id IS NULL"),
    )
    op.drop_index(op.f("ix_stored_files_uploaded_by_id"), table_name="stored_files")
    op.drop_index(op.f("ix_stored_files_message_id"), table_name="stored_files")
    op.drop_index(op.f("ix_stored_files_note_id"), table_name="stored_files")
    op.drop_index(op.f("ix_stored_files_storage_key"), table_name="stored_files")
    op.drop_index(op.f("ix_stored_files_public_id"), table_name="stored_files")
    op.drop_table("stored_files")
