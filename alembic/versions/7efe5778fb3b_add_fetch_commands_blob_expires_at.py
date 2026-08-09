"""capture the blob's expiry horizon on fetch commands (#253)

Revision ID: 7efe5778fb3b
Revises: e7c4b2a91f60
Create Date: 2026-08-09 19:05:33.461826

``BlobAvailableEvent.blob_expires_at`` (cannobserv#301) says when the blob stops
being retrievable at ``blob_uri``. Watcher echoes it onward on
``source_revision_observed`` under the same name, so Archiver can populate
``content_cache_expires_at`` honestly instead of deriving it from the issuer
contract's MUST-7 TTL — Replicator's policy, on a clock that runs from last
fetch reference, an event no consumer observes.

Nullable with no backfill, deliberately: NULL means "horizon unknown", which is
the truthful value for every command already applied before this column existed.
A backfill could only invent one.

Additive and safe in any order — the column is written by the ``content.blobs``
consumer and read by nothing until the publisher lands.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7efe5778fb3b"
down_revision: str | Sequence[str] | None = "e7c4b2a91f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "fetch_commands",
        sa.Column("blob_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("fetch_commands", "blob_expires_at")
