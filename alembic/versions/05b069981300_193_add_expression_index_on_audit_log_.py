"""#193 add expression index on audit_log payload->>'watched_item_id'.

``audit_log`` is append-only and grows unbounded. The WatchedItem-association
filter lives in the JSONB ``payload`` (the ``watch_id`` FK column was retired
in #191), so ``GET /api/v1/audit?watched_item_id=`` and the dashboard
"Recent Activity" panel both ran an unindexed expression scan that degrades to
a seq scan over time. Index the text-extraction the queries filter on, plus
``created_at DESC`` for the order-by every caller applies.

Revision ID: 05b069981300
Revises: b3c4d5e6f7a8
Create Date: 2026-06-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "05b069981300"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_log_payload_watched_item_id",
        "audit_log",
        [sa.literal_column("(payload->>'watched_item_id')"), sa.literal_column("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_payload_watched_item_id", table_name="audit_log")
