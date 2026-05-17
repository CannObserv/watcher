"""add watched_items and notification templates (#160)

Revision ID: 66719b436658
Revises: a0f5414820b7
Create Date: 2026-05-17 00:50:06.645444

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "66719b436658"
down_revision: str | Sequence[str] | None = "a0f5414820b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add watched_items + watched_item_notification_templates.

    First step of the InfoItem-first Watch model (#160). Additive only: no
    existing tables modified. Subsequent migrations reshape `watches` to
    point at WatchedItems.
    """
    op.create_table(
        "watched_items",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("info_item_id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("default_schedule_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("default_content_type", sa.String(length=20), nullable=True),
        sa.Column("default_tags", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_watched_items_info_item_id"),
        "watched_items",
        ["info_item_id"],
        unique=True,
    )
    op.create_table(
        "watched_item_notification_templates",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=True),
        sa.Column("channel_hint", sa.String(length=50), nullable=False),
        sa.Column(
            "events",
            postgresql.ARRAY(sa.String(length=50)),
            server_default="{change_detected}",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("content_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("remote_channel_id", sa.String(length=26), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_watched_item_notification_templates_watched_item_id"),
        "watched_item_notification_templates",
        ["watched_item_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop watched_item_notification_templates then watched_items."""
    op.drop_index(
        op.f("ix_watched_item_notification_templates_watched_item_id"),
        table_name="watched_item_notification_templates",
    )
    op.drop_table("watched_item_notification_templates")
    op.drop_index(op.f("ix_watched_items_info_item_id"), table_name="watched_items")
    op.drop_table("watched_items")
