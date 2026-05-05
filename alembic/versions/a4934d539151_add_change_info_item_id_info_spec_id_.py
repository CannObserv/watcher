"""add change info_item_id, info_spec_id, fingerprint columns

Revision ID: a4934d539151
Revises: abf62e0c9ffd
Create Date: 2026-05-05 01:28:13.336995

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4934d539151"
down_revision: str | Sequence[str] | None = "abf62e0c9ffd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — add InfoItem linkage + fingerprint columns to changes."""
    op.add_column("changes", sa.Column("info_item_id", sa.String(length=26), nullable=True))
    op.add_column("changes", sa.Column("info_spec_id", sa.String(length=26), nullable=True))
    op.add_column("changes", sa.Column("previous_fingerprint", sa.BigInteger(), nullable=True))
    op.add_column("changes", sa.Column("current_fingerprint", sa.BigInteger(), nullable=True))
    op.create_index(
        op.f("ix_changes_info_item_id"),
        "changes",
        ["info_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_changes_info_item_id_detected_at",
        "changes",
        [sa.column("info_item_id"), sa.column("detected_at").desc()],
    )


def downgrade() -> None:
    """Downgrade schema — drop InfoItem linkage columns + indices."""
    op.drop_index("ix_changes_info_item_id_detected_at", table_name="changes")
    op.drop_index(op.f("ix_changes_info_item_id"), table_name="changes")
    op.drop_column("changes", "current_fingerprint")
    op.drop_column("changes", "previous_fingerprint")
    op.drop_column("changes", "info_spec_id")
    op.drop_column("changes", "info_item_id")
