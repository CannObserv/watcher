"""add visual_change_score to change

Revision ID: b007cfbd885a
Revises: 45b7d0001fc7
Create Date: 2026-04-02 21:28:42.544964

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b007cfbd885a'
down_revision: Union[str, Sequence[str], None] = '45b7d0001fc7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('changes', sa.Column('visual_change_score', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('changes', 'visual_change_score')
