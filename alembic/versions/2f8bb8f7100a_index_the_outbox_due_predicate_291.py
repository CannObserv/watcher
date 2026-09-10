"""index the outbox due predicate (#291)

Revision ID: 2f8bb8f7100a
Revises: 10783d8a2405
Create Date: 2026-09-10

``pending_archiver_sync`` had indexes on its two foreign keys and nothing on
``next_attempt_at``, which is what both hot queries filter:
``select_due`` every minute, and ``clear_backoffs`` on every pass that
publishes something (#291). Free while the table is near-empty — a row is
deleted on success, so that is its normal state — but the case worth indexing
for is exactly the one #291 exists for: a broker outage has grown the backlog
into the thousands, and every tick then scans it twice.

Partial on ``dead_lettered_at IS NULL`` because both queries carry that
predicate: a dead-lettered row is terminal, never a candidate for either, and
so costs nothing to leave out.

**Both key columns**, matching ``select_due``'s ``ORDER BY next_attempt_at,
id``. The second is not padding. A bulk backoff clear stamps one timestamp
across the outbox, which collapses the whole backlog into a single sort group:
measured at 5 000 tied rows, ``(next_attempt_at)`` alone plans an Incremental
Sort that reads all 5 000 to return a 100-row batch, while
``(next_attempt_at, id)`` is a plain index scan of exactly 100. The tie case is
the recovery case, so the index has to cover it.

**Plain ``CREATE INDEX``, not ``CONCURRENTLY``.** The lock is ``SHARE`` on one
table that normally holds single-digit rows, and the drain that would contend
for it runs for milliseconds once a minute. ``CONCURRENTLY`` cannot run inside
the transaction alembic wraps each migration in, and the complexity buys
nothing at this size.

No deploy-order constraint: an index is invisible to the ORM's SELECTs, so
either order is safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2f8bb8f7100a"
down_revision: str | Sequence[str] | None = "10783d8a2405"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the partial index serving select_due and clear_backoffs."""
    op.create_index(
        "ix_pending_archiver_sync_due",
        "pending_archiver_sync",
        ["next_attempt_at", "id"],
        unique=False,
        postgresql_where=sa.text("dead_lettered_at IS NULL"),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop it. The queries fall back to the scan they did before."""
    op.drop_index(
        "ix_pending_archiver_sync_due",
        table_name="pending_archiver_sync",
        postgresql_where=sa.text("dead_lettered_at IS NULL"),
        if_exists=True,
    )
