"""add partial index for unpublished changes drain

Revision ID: abf62e0c9ffd
Revises: abf62e0c9ffc
Create Date: 2026-05-04 05:36:18.356517

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'abf62e0c9ffd'
down_revision: Union[str, Sequence[str], None] = 'abf62e0c9ffc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add partial index over unpublished changes for the drain hot path."""
    op.create_index(
        "ix_changes_unpublished_detected_at",
        "changes",
        ["detected_at"],
        postgresql_where=sa.text("published_to_bus_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_changes_unpublished_detected_at", table_name="changes")
