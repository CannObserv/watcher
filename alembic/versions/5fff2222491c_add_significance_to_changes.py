"""add significance to changes

Revision ID: 5fff2222491c
Revises: 995f4174acd9
Create Date: 2026-03-23 04:42:10.288057

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5fff2222491c'
down_revision: Union[str, Sequence[str], None] = '995f4174acd9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable significance float column to changes."""
    op.add_column('changes', sa.Column('significance', sa.Float(), nullable=True))


def downgrade() -> None:
    """Remove significance column from changes."""
    op.drop_column('changes', 'significance')
