"""#218 add audit_log scale indexes: event_type composite + created_at.

``audit_log`` is append-only and grows unbounded. The Audit Log page (#215, #217)
runs three queries per load, two of which had no supporting index:

  - the dominant unfiltered list ``ORDER BY created_at DESC LIMIT n`` → seq scan
    + top-N sort. Add a ``created_at DESC`` index (the #193 index leads with the
    JSONB payload key, so it can't serve this).
  - the ``event_type IN (...) ORDER BY created_at DESC`` filtered list *and* the
    ``SELECT DISTINCT event_type ORDER BY event_type`` chip vocabulary (#217) →
    seq scan. Add one composite ``(event_type, created_at DESC)``: the leading
    column serves the DISTINCT via an index-only scan + Unique (no heap, free
    sort), and the pair serves the filtered/ordered list.

Revision ID: c5d6e7f8a9b0
Revises: a3b1c2d4e5f6
Create Date: 2026-06-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "a3b1c2d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_log_event_type",
        "audit_log",
        ["event_type", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_log_created_at",
        "audit_log",
        [sa.literal_column("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_event_type", table_name="audit_log")
