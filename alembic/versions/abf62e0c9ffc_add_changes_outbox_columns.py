"""add changes outbox columns

Revision ID: abf62e0c9ffc
Revises: 96641996744b
Create Date: 2026-05-04 03:59:19.715946

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'abf62e0c9ffc'
down_revision: Union[str, Sequence[str], None] = '96641996744b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('changes', sa.Column('published_to_bus_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('changes', sa.Column('bus_message_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('changes', 'bus_message_id')
    op.drop_column('changes', 'published_to_bus_at')
