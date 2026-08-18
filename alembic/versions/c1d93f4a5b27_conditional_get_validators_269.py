"""conditional-GET validator state (#269 parts 2-3)

Revision ID: c1d93f4a5b27
Revises: f4a8b26c9d31
Create Date: 2026-08-18 16:00:00.000000

Two column sets, one feature.

On ``fetch_commands``: ``etag`` / ``last_modified`` are the validators a blob
fact returned (they have arrived on every fact since replicator#10 and were
dropped until now), and ``request_etag`` / ``request_last_modified`` are what the
command *asked*, snapshotted at the occasion. The snapshot is not redundant with
the item's pair: the pending-publish sweep republishes from this row alone, so
without it a republish could carry different headers than the original command —
and a 304 with no record of which validator earned it is undiagnosable.

On ``watched_items``: the replayable pair itself, plus ``validator_source_key``
(URL + source_specs + extraction generation at the time the pair was stored) and
``last_full_fetch_at`` (when bytes last arrived — 304 cycles deliberately do not
advance it). Those two are what stop a 304 streak from inheriting a fingerprint
nothing recomputed.

NULL backfill is truthful everywhere: no prior command carried a validator, and
no prior fact recorded one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d93f4a5b27"
down_revision: str | None = "f4a8b26c9d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fetch_commands", sa.Column("etag", sa.Text(), nullable=True))
    op.add_column("fetch_commands", sa.Column("last_modified", sa.Text(), nullable=True))
    op.add_column("fetch_commands", sa.Column("request_etag", sa.Text(), nullable=True))
    op.add_column("fetch_commands", sa.Column("request_last_modified", sa.Text(), nullable=True))

    op.add_column("watched_items", sa.Column("etag", sa.Text(), nullable=True))
    op.add_column("watched_items", sa.Column("last_modified", sa.Text(), nullable=True))
    op.add_column("watched_items", sa.Column("validator_source_key", sa.Text(), nullable=True))
    op.add_column(
        "watched_items",
        sa.Column("last_full_fetch_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("watched_items", "last_full_fetch_at")
    op.drop_column("watched_items", "validator_source_key")
    op.drop_column("watched_items", "last_modified")
    op.drop_column("watched_items", "etag")

    op.drop_column("fetch_commands", "request_last_modified")
    op.drop_column("fetch_commands", "request_etag")
    op.drop_column("fetch_commands", "last_modified")
    op.drop_column("fetch_commands", "etag")
