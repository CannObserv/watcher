"""add_notification_event_types_and_reshape_configs

Revision ID: ed3a126a39e5
Revises: b007cfbd885a
Create Date: 2026-04-04 15:13:00.613380

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'ed3a126a39e5'
down_revision: Union[str, Sequence[str], None] = 'b007cfbd885a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Create notification_event_types catalog table
    op.create_table(
        "notification_event_types",
        sa.Column("code", sa.String(50), primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )

    # 2. Seed event types
    op.bulk_insert(
        sa.table(
            "notification_event_types",
            sa.column("code", sa.String),
            sa.column("label", sa.String),
            sa.column("description", sa.String),
            sa.column("is_active", sa.Boolean),
        ),
        [
            {"code": "change_detected", "label": "Change Detected",
             "description": "Content change detected during a watch check", "is_active": True},
            {"code": "watch_error", "label": "Watch Error",
             "description": "Watch check failed (first failure after success or unknown)", "is_active": True},
            {"code": "watch_recovered", "label": "Watch Recovered",
             "description": "Watch check succeeded after one or more consecutive failures", "is_active": True},
            {"code": "watch_created", "label": "Watch Created",
             "description": "A new watch was created", "is_active": True},
            {"code": "watch_paused", "label": "Watch Paused",
             "description": "A watch was paused (deactivated)", "is_active": True},
            {"code": "watch_resumed", "label": "Watch Resumed",
             "description": "A watch was resumed (reactivated)", "is_active": True},
        ],
    )

    # 3. Reshape notification_configs: drop old columns, add new ones
    # Pre-production: truncate first for safety
    op.execute("TRUNCATE TABLE notification_configs")
    op.drop_column("notification_configs", "channel")
    op.drop_column("notification_configs", "config")
    op.add_column(
        "notification_configs",
        sa.Column("apprise_url", sa.Text, nullable=False),
    )
    op.add_column(
        "notification_configs",
        sa.Column("channel_hint", sa.String(50), nullable=False),
    )
    op.add_column(
        "notification_configs",
        sa.Column(
            "events",
            postgresql.ARRAY(sa.String(50)),
            nullable=False,
            server_default="{change_detected}",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("notification_configs", "events")
    op.drop_column("notification_configs", "channel_hint")
    op.drop_column("notification_configs", "apprise_url")
    op.add_column("notification_configs", sa.Column("config", postgresql.JSONB, server_default="{}"))
    op.add_column("notification_configs", sa.Column("channel", sa.String(20), nullable=False, server_default="webhook"))
    op.drop_table("notification_event_types")
