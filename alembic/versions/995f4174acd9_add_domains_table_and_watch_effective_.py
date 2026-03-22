"""add domains table and watch effective fields

Revision ID: 995f4174acd9
Revises: f0083c076bea
Create Date: 2026-03-22 20:36:58.523671

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '995f4174acd9'
down_revision: Union[str, Sequence[str], None] = 'f0083c076bea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('domains',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=253), nullable=False),
    sa.Column('min_interval', sa.Float(), server_default='1.0', nullable=False),
    sa.Column('max_concurrency', sa.Integer(), server_default='2', nullable=False),
    sa.Column('current_interval', sa.Float(), server_default='1.0', nullable=False),
    sa.Column('last_request_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.add_column('watches', sa.Column('effective_url', sa.Text(), nullable=True))
    op.add_column('watches', sa.Column('effective_domain', sa.String(length=253), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('watches', 'effective_domain')
    op.drop_column('watches', 'effective_url')
    op.drop_table('domains')
