"""drop watches.info_item_id, make info_source_id not null

Revision ID: 96aba824f3f2
Revises: d27d332647f9
Create Date: 2026-05-13 22:55:44.461464

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '96aba824f3f2'
down_revision: Union[str, Sequence[str], None] = 'd27d332647f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop watches.info_item_id and make info_source_id NOT NULL."""
    # Drop index on info_item_id (no FK constraint exists; column was added without one)
    op.drop_index('ix_watches_info_item_id', table_name='watches')
    # Drop the column
    op.drop_column('watches', 'info_item_id')
    # Make info_source_id NOT NULL (all rows populated by migration script before this runs)
    op.alter_column('watches', 'info_source_id',
                    existing_type=sa.VARCHAR(length=26),
                    nullable=False)


def downgrade() -> None:
    """Restore info_item_id and make info_source_id nullable again."""
    # Re-nullable info_source_id
    op.alter_column('watches', 'info_source_id',
                    existing_type=sa.VARCHAR(length=26),
                    nullable=True)
    # Re-add info_item_id column (nullable to allow restoring data)
    op.add_column('watches', sa.Column('info_item_id', sa.VARCHAR(length=26), nullable=True))
    op.create_index('ix_watches_info_item_id', 'watches', ['info_item_id'])
