"""add decay_window to domains

Revision ID: 2b5909fdd2ca
Revises: 3a1b2c4d5e6f
Create Date: 2026-03-23 23:46:29.413031

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b5909fdd2ca'
down_revision: Union[str, Sequence[str], None] = '3a1b2c4d5e6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('domains', sa.Column('decay_window', sa.Float(), server_default='1800.0', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('domains', 'decay_window')
