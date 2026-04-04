"""add_watch_health_status

Revision ID: cc66e3656dfe
Revises: ed3a126a39e5
Create Date: 2026-04-04 15:15:16.378687

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cc66e3656dfe'
down_revision: Union[str, Sequence[str], None] = 'ed3a126a39e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "watches",
        sa.Column(
            "health_status",
            sa.String(10),
            nullable=False,
            server_default="unknown",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("watches", "health_status")
