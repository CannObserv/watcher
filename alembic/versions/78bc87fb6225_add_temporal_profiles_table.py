"""add temporal_profiles table

Revision ID: 78bc87fb6225
Revises: 9ea381c8ff08
Create Date: 2026-03-21 02:33:12.137942

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '78bc87fb6225'
down_revision: Union[str, Sequence[str], None] = '9ea381c8ff08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('temporal_profiles',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('watch_id', sa.String(length=26), nullable=False),
    sa.Column('profile_type', sa.String(length=20), nullable=False),
    sa.Column('reference_date', sa.Date(), nullable=True),
    sa.Column('date_range_start', sa.Date(), nullable=True),
    sa.Column('date_range_end', sa.Date(), nullable=True),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('post_action', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['watch_id'], ['watches.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('temporal_profiles')
