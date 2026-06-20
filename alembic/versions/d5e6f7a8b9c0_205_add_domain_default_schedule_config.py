"""#205 add per-domain default check interval (3-tier schedule resolution)

Revision ID: d5e6f7a8b9c0
Revises: e1f2a3b4c5d6
Create Date: 2026-06-20 00:00:00.000000

Additive migration. ``domains.default_schedule_config`` holds the operator's
desired check cadence for items on the domain (a schedule_config interval
string), distinct from the rate-limiter ``min_interval``. ``watched_items.
domain_default_schedule_config`` is its denormalized copy, the Domain tier of
the WatchedItem -> Domain -> system resolution chain. Both nullable JSONB; an
unset value is SQL NULL (none_as_null on the ORM side), so no server default.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the Domain cadence column and its WatchedItem denormalized copy."""
    op.add_column(
        "domains",
        sa.Column(
            "default_schedule_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "watched_items",
        sa.Column(
            "domain_default_schedule_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the per-domain cadence columns."""
    op.drop_column("watched_items", "domain_default_schedule_config")
    op.drop_column("domains", "default_schedule_config")
