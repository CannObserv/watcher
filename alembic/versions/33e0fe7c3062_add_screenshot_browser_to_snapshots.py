"""add screenshot_browser to snapshots

Revision ID: 33e0fe7c3062
Revises: e1922dc1c1db
Create Date: 2026-03-31 18:40:40.606777

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '33e0fe7c3062'
down_revision: Union[str, Sequence[str], None] = 'e1922dc1c1db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('snapshots', sa.Column('screenshot_browser', sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('snapshots', 'screenshot_browser')
