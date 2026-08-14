"""watched_items.last_observed_at (#264)

Revision ID: a9d40c7e1b52
Revises: b3f1a7c50d94
Create Date: 2026-08-14 12:00:00.000000

Observation freshness for the ``info.watch-status`` return leg. Advances only
when a cycle's extraction succeeded — changed or unchanged both count — where
``last_checked_at`` advances on every outcome because it is a scheduling
anti-thrash device (#168). The distinction is what lets Archiver's registry
say "content verified current as of T" (written through to its durable
``info_sources.last_observed_at``) instead of conflating *verified same* with
*never looked*. NULL backfill is truthful: no prior cycle recorded which
outcomes were observations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d40c7e1b52"
down_revision: str | None = "b3f1a7c50d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "watched_items",
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("watched_items", "last_observed_at")
