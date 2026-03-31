"""add screenshot_path to snapshots

Revision ID: e1922dc1c1db
Revises: 38ea368b30b2
Create Date: 2026-03-31 16:29:19.985202

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e1922dc1c1db'
down_revision: Union[str, Sequence[str], None] = '38ea368b30b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('snapshots', sa.Column('screenshot_path', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('snapshots', 'screenshot_path')
