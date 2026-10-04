"""processor-decided extraction (#326)

Revision ID: c3f9a1d27b84
Revises: 2bc94dabe269
Create Date: 2026-10-03

Three changes for ``WATCHER_EXTRACT_MODE=processor``:

* ``watched_items.processor_version`` — the extraction identity of the item's
  latest successful outcome: Option A's comparison base and, processor-decided,
  the generation half of ``validator_source_key``. **Not backfilled** (CR 1):
  the latest revision's version is the extractor's at the last *change*, which
  can trail the installed one (0.19.4 → 0.19.7 between #324 and #325), and
  read as the base it would turn the next real change into a silent
  re-baseline. NULL is *unknown* — notify as before — and every successful
  local or shadow check writes the true value, unchanged ones included, well
  before any switch to ``processor``.
* ``ix_fetch_commands_open`` gains ``'processing'`` — the new open status. Its
  predicate must cover every open status for the scheduling gate's lookup to
  use it; correctness never depended on it.
* ``ix_process_commands_read_past`` — the reaper's read-past query scanned the
  table (#325 CR 11).

No ordering hazard: the old code never reads the new column and never writes
``processing``. Standard order — upgrade, then restart.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f9a1d27b84"
down_revision: str | None = "2bc94dabe269"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_BEFORE = "status IN ('pending_publish', 'in_flight')"
_OPEN_AFTER = "status IN ('pending_publish', 'in_flight', 'processing')"


def upgrade() -> None:
    """Add the column; widen the open index; add the read-past index."""
    op.add_column("watched_items", sa.Column("processor_version", sa.Text(), nullable=True))
    op.drop_index(
        "ix_fetch_commands_open",
        table_name="fetch_commands",
        postgresql_where=sa.text(_OPEN_BEFORE),
    )
    op.create_index(
        "ix_fetch_commands_open",
        "fetch_commands",
        ["watched_item_id"],
        unique=False,
        postgresql_where=sa.text(_OPEN_AFTER),
    )
    op.create_index(
        "ix_process_commands_read_past",
        "process_commands",
        ["published_at"],
        unique=False,
        postgresql_where=sa.text("fact_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Reverse the three changes."""
    op.drop_index(
        "ix_process_commands_read_past",
        table_name="process_commands",
        postgresql_where=sa.text("fact_at IS NOT NULL"),
    )
    op.drop_index(
        "ix_fetch_commands_open",
        table_name="fetch_commands",
        postgresql_where=sa.text(_OPEN_AFTER),
    )
    op.create_index(
        "ix_fetch_commands_open",
        "fetch_commands",
        ["watched_item_id"],
        unique=False,
        postgresql_where=sa.text(_OPEN_BEFORE),
    )
    op.drop_column("watched_items", "processor_version")
