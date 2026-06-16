"""Re-key notification configs + refs to watched_item_id — #191 collapse phase 2.

Pre-prod: tables are truncated rather than backfilled.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # watch_notification_configs: watch_id -> watched_item_id
    op.execute("TRUNCATE TABLE watch_notification_configs")
    op.drop_constraint(
        "notification_configs_watch_id_fkey",
        "watch_notification_configs",
        type_="foreignkey",
    )
    op.drop_column("watch_notification_configs", "watch_id")
    op.add_column(
        "watch_notification_configs",
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
    )
    op.create_foreign_key(
        "watch_notification_configs_watched_item_id_fkey",
        "watch_notification_configs",
        "watched_items",
        ["watched_item_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # watch_nc_refs: composite PK (watch_id, template_id) -> (watched_item_id, template_id)
    op.execute("TRUNCATE TABLE watch_nc_refs")
    op.drop_constraint("watch_nc_refs_watch_id_fkey", "watch_nc_refs", type_="foreignkey")
    op.drop_column("watch_nc_refs", "watch_id")
    op.add_column(
        "watch_nc_refs",
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
    )
    op.create_primary_key("watch_nc_refs_pkey", "watch_nc_refs", ["watched_item_id", "template_id"])
    op.create_foreign_key(
        "watch_nc_refs_watched_item_id_fkey",
        "watch_nc_refs",
        "watched_items",
        ["watched_item_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.execute("TRUNCATE TABLE watch_nc_refs")
    op.drop_constraint("watch_nc_refs_watched_item_id_fkey", "watch_nc_refs", type_="foreignkey")
    op.drop_constraint("watch_nc_refs_pkey", "watch_nc_refs", type_="primary")
    op.drop_column("watch_nc_refs", "watched_item_id")
    op.add_column("watch_nc_refs", sa.Column("watch_id", sa.String(length=26), nullable=False))
    op.create_primary_key("watch_nc_refs_pkey", "watch_nc_refs", ["watch_id", "template_id"])
    op.create_foreign_key(
        "watch_nc_refs_watch_id_fkey",
        "watch_nc_refs",
        "watches",
        ["watch_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.execute("TRUNCATE TABLE watch_notification_configs")
    op.drop_constraint(
        "watch_notification_configs_watched_item_id_fkey",
        "watch_notification_configs",
        type_="foreignkey",
    )
    op.drop_column("watch_notification_configs", "watched_item_id")
    op.add_column(
        "watch_notification_configs",
        sa.Column("watch_id", sa.String(length=26), nullable=False),
    )
    op.create_foreign_key(
        "watch_notification_configs_watch_id_fkey",
        "watch_notification_configs",
        "watches",
        ["watch_id"],
        ["id"],
        ondelete="CASCADE",
    )
