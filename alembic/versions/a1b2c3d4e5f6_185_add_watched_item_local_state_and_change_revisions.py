"""#185 Phase A: add watched_item local state columns, change_revisions, pending_archiver_sync

Revision ID: a1b2c3d4e5f6
Revises: fc32c9a4bc9e
Create Date: 2026-06-08 00:00:00.000000

Additive migration — no existing rows are modified. NOT NULL columns have no
server default because the pre-production database has no rows. info_item_id
becomes nullable (standalone WatchedItems have no InfoItem); the former unique
index is replaced by a partial unique index WHERE info_item_id IS NOT NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "774d506c6267"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add local state columns to watched_items; create change_revisions and pending_archiver_sync."""
    # --- watched_items: new columns ---
    # Server defaults handle existing rows; pipeline will populate real values on next run.
    op.add_column(
        "watched_items",
        sa.Column("effective_url", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "watched_items",
        sa.Column(
            "source_specs",
            postgresql.ARRAY(postgresql.JSONB(astext_type=sa.Text())),
            nullable=False,
            server_default=sa.text("ARRAY[]::jsonb[]"),
        ),
    )
    op.add_column(
        "watched_items",
        sa.Column("archiver_info_source_id", sa.String(length=26), nullable=True),
    )
    op.add_column(
        "watched_items",
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "watched_items",
        sa.Column(
            "health_status",
            sa.String(length=10),
            nullable=True,
            server_default="unknown",
        ),
    )

    # --- watched_items: make info_item_id nullable, replace unique index with partial ---
    op.drop_index("ix_watched_items_info_item_id", table_name="watched_items")
    op.alter_column("watched_items", "info_item_id", existing_type=sa.String(length=26), nullable=True)
    op.create_index(
        "ix_watched_items_info_item_id",
        "watched_items",
        ["info_item_id"],
        unique=True,
        postgresql_where=sa.text("info_item_id IS NOT NULL"),
    )

    # --- change_revisions ---
    op.create_table(
        "change_revisions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("content_fingerprint", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("archiver_revision_id", sa.String(length=26), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["watched_item_id"],
            ["watched_items.id"],
            name="fk_change_revisions_watched_item_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_change_revisions"),
    )
    op.create_index(
        "ix_change_revisions_watched_item_captured_at",
        "change_revisions",
        ["watched_item_id", sa.text("captured_at DESC")],
        unique=False,
    )

    # --- pending_archiver_sync ---
    op.create_table(
        "pending_archiver_sync",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("change_revision_id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("content_cache_uri", sa.Text(), nullable=False),
        sa.Column("content_cache_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["change_revision_id"],
            ["change_revisions.id"],
            name="fk_pending_archiver_sync_change_revision_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["watched_item_id"],
            ["watched_items.id"],
            name="fk_pending_archiver_sync_watched_item_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "change_revision_id",
            name="uq_pending_archiver_sync_change_revision_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pending_archiver_sync"),
    )
    op.create_index(
        "ix_pending_archiver_sync_next_attempt",
        "pending_archiver_sync",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("attempts < 10"),
    )


def downgrade() -> None:
    """Reverse: drop new tables and columns; restore info_item_id uniqueness."""
    op.drop_table("pending_archiver_sync")
    op.drop_table("change_revisions")

    op.drop_column("watched_items", "health_status")
    op.drop_column("watched_items", "last_changed_at")
    op.drop_column("watched_items", "archiver_info_source_id")
    op.drop_column("watched_items", "source_specs")
    op.drop_column("watched_items", "effective_url")

    # Restore info_item_id as NOT NULL unique.
    op.drop_index("ix_watched_items_info_item_id", table_name="watched_items")
    op.alter_column("watched_items", "info_item_id", existing_type=sa.String(length=26), nullable=False)
    op.create_index(
        op.f("ix_watched_items_info_item_id"),
        "watched_items",
        ["info_item_id"],
        unique=True,
    )
