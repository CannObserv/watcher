"""add domain notes and archived_at columns

Revision ID: 38ea368b30b2
Revises: 2b5909fdd2ca
Create Date: 2026-03-25 16:52:24.793351

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '38ea368b30b2'
down_revision: Union[str, Sequence[str], None] = '2b5909fdd2ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('domains', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('domains', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('domains', 'archived_at')
    op.drop_column('domains', 'notes')
