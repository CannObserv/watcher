"""add last_changed_at to watches

Revision ID: fa835a9e496d
Revises: 9fe9b95a506f
Create Date: 2026-04-14 22:05:14.568667

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fa835a9e496d"
down_revision: Union[str, Sequence[str], None] = "9fe9b95a506f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add last_changed_at column to watches table."""
    op.add_column("watches", sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Remove last_changed_at column from watches table."""
    op.drop_column("watches", "last_changed_at")
