"""add tags and description to watches

Revision ID: 917d378da82f
Revises: fa835a9e496d
Create Date: 2026-04-14 22:46:13.637660

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "917d378da82f"
down_revision: Union[str, Sequence[str], None] = "fa835a9e496d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tags (ARRAY) and description (Text) columns to watches."""
    op.add_column("watches", sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column("watches", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove tags and description columns from watches."""
    op.drop_column("watches", "description")
    op.drop_column("watches", "tags")
