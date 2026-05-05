"""add watches.info_item_id (nullable)

Revision ID: 9e86f9e4d704
Revises: a4934d539151
Create Date: 2026-05-05 01:56:07.813981

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e86f9e4d704"
down_revision: str | Sequence[str] | None = "a4934d539151"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — add nullable info_item_id with cross-schema FK."""
    op.add_column(
        "watches",
        sa.Column("info_item_id", sa.String(length=26), nullable=True),
    )
    op.create_index(
        op.f("ix_watches_info_item_id"),
        "watches",
        ["info_item_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_watches_info_item_id",
        "watches",
        "info_items",
        ["info_item_id"],
        ["info_item_id"],
        referent_schema="information",
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Downgrade schema — drop FK, index, and column."""
    op.drop_constraint("fk_watches_info_item_id", "watches", type_="foreignkey")
    op.drop_index(op.f("ix_watches_info_item_id"), table_name="watches")
    op.drop_column("watches", "info_item_id")
