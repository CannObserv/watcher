"""create last_known_revisions cache

Revision ID: 6bee3582aedc
Revises: d4286311c383
Create Date: 2026-05-13 18:53:57.559771

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6bee3582aedc"
down_revision: str | Sequence[str] | None = "d4286311c383"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "last_known_revisions",
        sa.Column("info_source_id", sa.String(length=26), nullable=False),
        sa.Column("content_fingerprint", sa.Text(), nullable=False),
        sa.Column("source_revision_id", sa.String(length=26), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("info_source_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("last_known_revisions")
