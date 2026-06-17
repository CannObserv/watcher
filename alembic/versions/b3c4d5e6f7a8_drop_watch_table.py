"""Drop the watches table + audit_log.watch_id — #191 collapse phase 4.

WatchedItem is now the single monitored entity. The only remaining FK to
``watches`` is ``audit_log.watch_id`` (the WatchedItem association moved into
the audit ``payload`` JSONB as ``watched_item_id``), so it is dropped first.

Pre-prod: no data preservation. Downgrade best-effort recreates the table shape.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # audit_log.watch_id FK + column — the only remaining reference to watches.
    op.drop_constraint("audit_log_watch_id_fkey", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "watch_id")

    op.drop_table("watches")


def downgrade() -> None:
    op.create_table(
        "watches",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=20), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("suspended_by_domain", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("tags", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["watched_item_id"],
            ["watched_items.id"],
            name="fk_watches_watched_item_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_watches_watched_item_id", "watches", ["watched_item_id"])

    op.add_column("audit_log", sa.Column("watch_id", sa.String(length=26), nullable=True))
    op.create_foreign_key(
        "audit_log_watch_id_fkey",
        "audit_log",
        "watches",
        ["watch_id"],
        ["id"],
        ondelete="SET NULL",
    )
