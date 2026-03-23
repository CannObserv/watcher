"""add is_archived column to watches

Revision ID: 3a1b2c4d5e6f
Revises: f0083c076bea
Create Date: 2026-03-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3a1b2c4d5e6f'
down_revision: str | Sequence[str] | None = 'f0083c076bea'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add is_archived column to watches table, default False."""
    op.add_column(
        'watches',
        sa.Column(
            'is_archived',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ),
    )


def downgrade() -> None:
    """Remove is_archived column from watches table."""
    op.drop_column('watches', 'is_archived')
