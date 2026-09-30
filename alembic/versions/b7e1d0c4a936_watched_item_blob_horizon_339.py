"""watched_items.blob_expires_at (#339)

The retrievability horizon of the blob behind the item's latest full fetch,
written by ``stamp_full_fetch`` from the same fact as ``last_full_fetch_at``.
``replayable_validators`` stops replaying a pair once half of it has elapsed:
a 304 produces no blob and so no #293 renewal, and Replicator's horizon is its
setting, not a watcher constant.

Nullable, no backfill: NULL is an unknown horizon, which is not replayable, so
each existing item's next command fetches in full and populates the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e1d0c4a936"
down_revision: str | None = "a3f9c2d17e48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "watched_items",
        sa.Column("blob_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("watched_items", "blob_expires_at")
