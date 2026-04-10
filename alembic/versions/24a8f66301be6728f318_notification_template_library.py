"""notification template library

Revision ID: 24a8f66301be6728f318
Revises: 6dbe1199d3a0
Create Date: 2026-04-10
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "24a8f66301be6728f318"
down_revision: Union[str, Sequence[str], None] = "6dbe1199d3a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename existing table
    op.rename_table("notification_configs", "watch_notification_configs")

    # 2. Create notification_templates
    op.create_table(
        "notification_templates",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("apprise_url", sa.Text(), nullable=False),
        sa.Column("channel_hint", sa.String(50), nullable=False),
        sa.Column(
            "events",
            postgresql.ARRAY(sa.String(50)),
            nullable=False,
            server_default=sa.text("ARRAY['change_detected']::varchar[]"),
        ),
        sa.Column("is_global_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # 3. Create watch_nc_refs junction
    op.create_table(
        "watch_nc_refs",
        sa.Column("watch_id", sa.String(26), nullable=False),
        sa.Column("template_id", sa.String(26), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["watch_id"], ["watches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["notification_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("watch_id", "template_id"),
    )

    # 4. Create domain_nc_refs junction
    op.create_table(
        "domain_nc_refs",
        sa.Column("domain_name", sa.String(253), nullable=False),
        sa.Column("template_id", sa.String(26), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["domain_name"], ["domains.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["notification_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("domain_name", "template_id"),
    )


def downgrade() -> None:
    op.drop_table("domain_nc_refs")
    op.drop_table("watch_nc_refs")
    op.drop_table("notification_templates")
    op.rename_table("watch_notification_configs", "notification_configs")
