"""Re-key temporal_profiles to watched_item_id (1:1) — #191 collapse phase 1.

Pre-prod: temporal_profiles is truncated rather than backfilled.

Revision ID: f1a2b3c4d5e6
Revises: e1e9a0542242
Create Date: 2026-06-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e1e9a0542242"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pre-prod: no data migration — drop existing rows tied to the old key.
    op.execute("TRUNCATE TABLE temporal_profiles")
    op.drop_constraint(
        "temporal_profiles_watch_id_fkey", "temporal_profiles", type_="foreignkey"
    )
    op.drop_column("temporal_profiles", "watch_id")
    op.add_column(
        "temporal_profiles",
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
    )
    op.create_foreign_key(
        "temporal_profiles_watched_item_id_fkey",
        "temporal_profiles",
        "watched_items",
        ["watched_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_temporal_profiles_watched_item_id",
        "temporal_profiles",
        ["watched_item_id"],
    )


def downgrade() -> None:
    op.execute("TRUNCATE TABLE temporal_profiles")
    op.drop_constraint(
        "uq_temporal_profiles_watched_item_id", "temporal_profiles", type_="unique"
    )
    op.drop_constraint(
        "temporal_profiles_watched_item_id_fkey", "temporal_profiles", type_="foreignkey"
    )
    op.drop_column("temporal_profiles", "watched_item_id")
    op.add_column(
        "temporal_profiles",
        sa.Column("watch_id", sa.String(length=26), nullable=False),
    )
    op.create_foreign_key(
        "temporal_profiles_watch_id_fkey",
        "temporal_profiles",
        "watches",
        ["watch_id"],
        ["id"],
        ondelete="CASCADE",
    )
