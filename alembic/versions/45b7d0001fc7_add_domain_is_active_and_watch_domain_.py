"""add domain is_active and watch domain_suspended

Revision ID: 45b7d0001fc7
Revises: 33e0fe7c3062
Create Date: 2026-04-01 21:47:55.899419

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '45b7d0001fc7'
down_revision: Union[str, Sequence[str], None] = '33e0fe7c3062'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('domains', sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False))
    op.add_column('watches', sa.Column('domain_suspended', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('watches', 'domain_suspended')
    op.drop_column('domains', 'is_active')
