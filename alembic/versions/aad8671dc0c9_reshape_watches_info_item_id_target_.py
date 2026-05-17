"""reshape watches: info_item_id + target_info_source_id + watched_item_id (#160)

Revision ID: aad8671dc0c9
Revises: 66719b436658
Create Date: 2026-05-17 04:25:00.561430

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aad8671dc0c9"
down_revision: str | Sequence[str] | None = "66719b436658"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reshape `watches` from InfoSource-1:1 to InfoItem-first (#160).

    Pre-prod: existing watches are wiped (TRUNCATE CASCADE) before the
    column shape changes — no data migration is attempted. Subscribers
    will be recreated post-cutover under the new WatchedItem hierarchy.

    Cross-schema FK targets (`information.info_items`,
    `information.info_sources`) are owned by Archiver's Alembic root.
    A stub `info_items` table is created here only if it is missing
    (dev DB has no Archiver tables); the test DB already has Archiver's
    real table from `_apply_archiver_migrations` and the IF NOT EXISTS
    guard keeps this a no-op there.
    """
    # Ensure the cross-schema FK target exists. No-op on the test DB where
    # Archiver's alembic has already provisioned the full table.
    op.execute("CREATE SCHEMA IF NOT EXISTS information")
    op.execute(
        "CREATE TABLE IF NOT EXISTS information.info_items (info_item_id varchar(26) PRIMARY KEY)"
    )

    # Pre-prod: wipe existing watches and dependent rows.
    op.execute("TRUNCATE TABLE watches CASCADE")

    # Drop the old InfoSource linkage.
    op.drop_constraint(op.f("fk_watches_info_source_id"), "watches", type_="foreignkey")
    op.drop_index(op.f("ix_watches_info_source_id"), table_name="watches")
    op.drop_column("watches", "info_source_id")
    op.drop_column("watches", "schedule_config")

    # Add the new shape.
    op.add_column("watches", sa.Column("info_item_id", sa.String(length=26), nullable=False))
    op.add_column(
        "watches",
        sa.Column("target_info_source_id", sa.String(length=26), nullable=True),
    )
    op.add_column("watches", sa.Column("watched_item_id", sa.String(length=26), nullable=False))
    op.alter_column(
        "watches",
        "content_type",
        existing_type=sa.VARCHAR(length=20),
        nullable=True,
    )

    op.create_index(
        op.f("ix_watches_info_item_id"),
        "watches",
        ["info_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watches_target_info_source_id"),
        "watches",
        ["target_info_source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watches_watched_item_id"),
        "watches",
        ["watched_item_id"],
        unique=False,
    )

    op.create_foreign_key(
        op.f("fk_watches_info_item_id"),
        "watches",
        "info_items",
        ["info_item_id"],
        ["info_item_id"],
        referent_schema="information",
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_watches_target_info_source_id"),
        "watches",
        "info_sources",
        ["target_info_source_id"],
        ["info_source_id"],
        referent_schema="information",
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_watches_watched_item_id"),
        "watches",
        "watched_items",
        ["watched_item_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Restore InfoSource-1:1 shape (pre-prod; dependent rows are wiped)."""
    # Pre-prod: wipe rows in the new shape so the NOT NULL info_source_id
    # column can be added back without a backfill.
    op.execute("TRUNCATE TABLE watches CASCADE")

    op.drop_constraint(op.f("fk_watches_watched_item_id"), "watches", type_="foreignkey")
    op.drop_constraint(op.f("fk_watches_target_info_source_id"), "watches", type_="foreignkey")
    op.drop_constraint(op.f("fk_watches_info_item_id"), "watches", type_="foreignkey")

    op.drop_index(op.f("ix_watches_watched_item_id"), table_name="watches")
    op.drop_index(op.f("ix_watches_target_info_source_id"), table_name="watches")
    op.drop_index(op.f("ix_watches_info_item_id"), table_name="watches")

    op.alter_column(
        "watches",
        "content_type",
        existing_type=sa.VARCHAR(length=20),
        nullable=False,
    )
    op.drop_column("watches", "watched_item_id")
    op.drop_column("watches", "target_info_source_id")
    op.drop_column("watches", "info_item_id")

    op.add_column(
        "watches",
        sa.Column(
            "schedule_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            autoincrement=False,
            nullable=False,
        ),
    )
    op.add_column(
        "watches",
        sa.Column(
            "info_source_id",
            sa.VARCHAR(length=26),
            autoincrement=False,
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_watches_info_source_id"),
        "watches",
        ["info_source_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_watches_info_source_id"),
        "watches",
        "info_sources",
        ["info_source_id"],
        ["info_source_id"],
        referent_schema="information",
        ondelete="RESTRICT",
    )
