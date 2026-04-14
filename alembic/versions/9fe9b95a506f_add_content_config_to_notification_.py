"""add content_config to notification tables

Revision ID: 9fe9b95a506f
Revises: ef3076ce30aa
Create Date: 2026-04-14 18:22:46.481565

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9fe9b95a506f"
down_revision: str | Sequence[str] | None = "ef3076ce30aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "notification_templates",
        sa.Column("content_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "watch_notification_configs",
        sa.Column("content_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("watch_notification_configs", "content_config")
    op.drop_column("notification_templates", "content_config")
