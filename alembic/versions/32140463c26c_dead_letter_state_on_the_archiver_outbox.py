"""dead-letter state on the archiver outbox; release the scratch columns (#253)

Revision ID: 32140463c26c
Revises: 9c1d4b7ea822
Create Date: 2026-08-10 02:05:00.000000

Two changes, both in service of the transport cutover.

**``dead_lettered_at``.** The drain now distinguishes a payload that can never be
built (deterministic — dead-letter at once) from a broker that is down
(transient — retry forever). The old ``attempts < 10`` filter in ``select_due``
was neither: it silently stopped selecting a row without marking it, so an outage
lasting ten backoffs abandoned revisions with no operator signal and nothing to
find them by. That filter is gone; giving up is now explicit and visible.

**``content_cache_uri`` / ``content_cache_expires_at`` become nullable.** They
described the scratch copy Watcher wrote so Archiver could read the bytes back
over HTTP. The durable-ish blob is Replicator's, at ``blob_uri``, and the
observation now carries that instead — nothing writes these two again.

They are **released, not dropped**, because no deploy order makes a drop safe in
one step: migrate-first breaks the running old code (it still INSERTs them, and
they are NOT NULL), and restart-first breaks the new code (it does not INSERT
them, and they are NOT NULL). Expand now, contract in a later migration once the
new code is live — the standard two-step, and the reason this one is safe in
either order.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "32140463c26c"
down_revision: str | Sequence[str] | None = "9c1d4b7ea822"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "pending_archiver_sync",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("pending_archiver_sync", "content_cache_uri", nullable=True)
    op.alter_column("pending_archiver_sync", "content_cache_expires_at", nullable=True)


def downgrade() -> None:
    """Downgrade schema.

    Restoring NOT NULL requires every row to carry both values; rows written by
    the bus producer never did, so they are deleted first. Safe because the
    outbox is a delivery queue, not a record — a lost row means one revision is
    not reported, and downgrading past the producer means it could not have been
    reported anyway.
    """
    op.execute(
        "DELETE FROM pending_archiver_sync "
        "WHERE content_cache_uri IS NULL OR content_cache_expires_at IS NULL"
    )
    op.alter_column("pending_archiver_sync", "content_cache_expires_at", nullable=False)
    op.alter_column("pending_archiver_sync", "content_cache_uri", nullable=False)
    op.drop_column("pending_archiver_sync", "dead_lettered_at")
