"""add last_checked_at to watched_items

Revision ID: fc32c9a4bc9e
Revises: aad8671dc0c9
Create Date: 2026-05-20 18:40:32.145817

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fc32c9a4bc9e'
down_revision: Union[str, Sequence[str], None] = 'aad8671dc0c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('watched_items', sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('watched_items', 'last_checked_at')
