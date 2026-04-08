"""add_title_to_notification_configs

Revision ID: 6dbe1199d3a0
Revises: cc66e3656dfe
Create Date: 2026-04-08 03:06:49.835548

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6dbe1199d3a0"
down_revision: Union[str, Sequence[str], None] = "cc66e3656dfe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("notification_configs", sa.Column("title", sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("notification_configs", "title")
