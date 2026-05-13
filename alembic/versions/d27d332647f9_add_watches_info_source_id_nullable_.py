"""add watches.info_source_id (nullable, transitional)

Revision ID: d27d332647f9
Revises: 7474a83e42fd
Create Date: 2026-05-13 20:25:30.294623

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd27d332647f9'
down_revision: Union[str, Sequence[str], None] = '7474a83e42fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add watches.info_source_id (nullable, cross-schema FK to information.info_sources)."""
    op.add_column('watches', sa.Column('info_source_id', sa.String(length=26), nullable=True))
    op.create_index(op.f('ix_watches_info_source_id'), 'watches', ['info_source_id'], unique=False)
    op.create_foreign_key(
        'fk_watches_info_source_id',
        'watches',
        'info_sources',
        ['info_source_id'],
        ['info_source_id'],
        referent_schema='information',
        ondelete='RESTRICT',
    )


def downgrade() -> None:
    """Drop watches.info_source_id column, index, and FK."""
    op.drop_constraint('fk_watches_info_source_id', 'watches', type_='foreignkey')
    op.drop_index(op.f('ix_watches_info_source_id'), table_name='watches')
    op.drop_column('watches', 'info_source_id')
