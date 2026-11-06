"""drop watched_items.last_error_notified_at (#376)

Revision ID: e2eab993d615
Revises: 7f3d3491e921
Create Date: 2026-11-06

F3 (#376) handed notifications to Archiver at T = 2026-11-06T21:00:00Z, and
part 2 deletes Watcher's dispatch path, #71's error reminder with it. The
column was that reminder's clock (``36358e2f5ad4``); nothing reads or
writes it any more, so its values are dead. The nightly backup holds them.
Downgrade restores the column ``36358e2f5ad4`` added, empty: its backfill
is not re-run, since nothing would read the stamps.

``IF EXISTS`` so the migration is idempotent on a database an operator
already hand-pruned. No grants step: ``watcher`` owns the table (#259).

**Deploy order: restart first, then migrate** — the reverse of the repo
default in AGENTS.md, same reasoning as ``f4a8b26c9d31`` (#261): the
*previous* release still maps the column, and SQLAlchemy names every mapped
column in its SELECTs, so dropping first fails every load of a WatchedItem
until the restart lands. Restart-first has no window: the new code never names
the column, and it is nullable, so the old schema accepts the new code's
INSERTs. Recorded in `docs/MIGRATIONS.md` → *Restart-before-migrate —
one-time, `e2eab993d615`*.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2eab993d615"
down_revision: str | Sequence[str] | None = "7f3d3491e921"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop #71's reminder clock; Watcher sends no reminder since F3."""
    op.execute("ALTER TABLE watched_items DROP COLUMN IF EXISTS last_error_notified_at")


def downgrade() -> None:
    """Restore the column as ``36358e2f5ad4`` added it, with no values."""
    op.add_column(
        "watched_items",
        sa.Column("last_error_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
