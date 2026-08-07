"""carry info_source_id on issued fetch commands (#252)

Revision ID: e7c4b2a91f60
Revises: d5a71c93e0f2
Create Date: 2026-08-07 00:00:00.000000

cannobserv#300 makes ``info_source_id`` required on all three content contracts,
so every ``content.fetch`` Watcher publishes must name the InfoSource it is for.
The value is snapshotted onto the command row rather than joined at publish time
— the pending-publish sweep holds only this row, no WatchedItem.

Hand-written: autogenerate would emit a bare NOT NULL add and fail on any
existing row. Backfill is total because #251 made
``watched_items.archiver_info_source_id`` NOT NULL, so add-nullable → backfill →
SET NOT NULL needs no fallback branch.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c4b2a91f60"
down_revision: str | Sequence[str] | None = "d5a71c93e0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "fetch_commands",
        sa.Column("info_source_id", sa.String(length=26), nullable=True),
    )
    op.execute(
        """
        UPDATE fetch_commands fc
        SET info_source_id = wi.archiver_info_source_id
        FROM watched_items wi
        WHERE wi.id = fc.watched_item_id
        """
    )
    op.alter_column(
        "fetch_commands",
        "info_source_id",
        existing_type=sa.String(length=26),
        nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("fetch_commands", "info_source_id")
