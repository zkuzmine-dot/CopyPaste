"""add keys to participants

Revision ID: c8e5a2f71d3b
Revises: b6e1d94c2f7a
Create Date: 2026-11-23 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e5a2f71d3b"
down_revision: str | Sequence[str] | None = "b6e1d94c2f7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("participants", sa.Column("key_commitment", sa.LargeBinary(), nullable=True))
    op.add_column("participants", sa.Column("public_key", sa.LargeBinary(), nullable=True))
    op.create_check_constraint(
        "ck_participants_key_commitment_length",
        "participants",
        "octet_length(key_commitment) = 32",
    )
    op.create_check_constraint(
        "ck_participants_public_key_length", "participants", "octet_length(public_key) = 65"
    )
    # Без ключей в канале больше ничего не отправить, а страницы открытых каналов обмениваться
    # ими не умеют. Каналы живут минуты, закрытые удалит обычная чистка
    op.execute("UPDATE channels SET closed_at = now() WHERE closed_at IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_participants_public_key_length", "participants", type_="check")
    op.drop_constraint("ck_participants_key_commitment_length", "participants", type_="check")
    op.drop_column("participants", "public_key")
    op.drop_column("participants", "key_commitment")
