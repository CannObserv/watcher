"""rename watched_items.info_item_id to archiver_info_item_id

Revision ID: e1e9a0542242
Revises: 2b7c99117877
Create Date: 2026-06-09 20:29:09.156021

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1e9a0542242'
down_revision: Union[str, Sequence[str], None] = '2b7c99117877'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("watched_items", "info_item_id", new_column_name="archiver_info_item_id")
    op.execute(
        "ALTER INDEX IF EXISTS ix_watched_items_info_item_id "
        "RENAME TO ix_watched_items_archiver_info_item_id"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "ALTER INDEX IF EXISTS ix_watched_items_archiver_info_item_id "
        "RENAME TO ix_watched_items_info_item_id"
    )
    op.alter_column("watched_items", "archiver_info_item_id", new_column_name="info_item_id")
