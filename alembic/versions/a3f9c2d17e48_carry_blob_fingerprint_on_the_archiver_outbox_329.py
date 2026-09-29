"""carry the raw-bytes digest on the archiver outbox (#329)

Revision ID: a3f9c2d17e48
Revises: ccc7de7cabf8
Create Date: 2026-09-29

One nullable ``TEXT`` column on ``pending_archiver_sync``: ``blob_fingerprint``,
Replicator's sha256 of the raw bytes, echoed from
``BlobAvailableEvent.content_fingerprint`` (already stored on
``fetch_commands.content_fingerprint``) so the drain can send it as
``SourceRevisionObservedEvent.blob_fingerprint`` (cannobserv#493). Archiver
persists each revision into Replicator's permanent store by it
(replicator#114, archiver#283).

No backfill: optional on the wire, so a NULL on a row enqueued before this
landed drains unchanged. The outbox was empty when this was written.

**Standard order: upgrade, then restart.** The new code writes the column on
every outbox insert and renewal upsert, so a process restarted onto it before
the upgrade would fail them. The reverse is safe: the old code never names it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3f9c2d17e48"
down_revision: str | Sequence[str] | None = "ccc7de7cabf8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``blob_fingerprint``, nullable, no default."""
    op.add_column(
        "pending_archiver_sync",
        sa.Column("blob_fingerprint", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop ``blob_fingerprint``."""
    op.drop_column("pending_archiver_sync", "blob_fingerprint")
