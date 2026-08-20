"""drop the four inert domain rate-limiter columns (#272)

Revision ID: 10783d8a2405
Revises: d4e28a71c630
Create Date: 2026-08-20

#241 step 5 retired the in-process ``DomainRateLimiter`` but kept its four
``domains`` columns so the retirement needed no destructive migration:
``max_concurrency``, ``decay_window``, ``current_interval`` (frozen 429-backoff
state), ``last_request_at`` (no writer at all). Nothing has read any of them
for behavior since; the same release as this migration removes the API
create/PATCH write sites and the ``DomainResponse`` fields. Adaptive backoff
is Replicator's (replicator#25).

**The drop is destructive and the data is worthless**: ``max_concurrency`` and
``decay_window`` were operator knobs nothing consulted, ``current_interval``
froze at the Phase-4 cutover, and ``last_request_at`` stopped being written
when the limiter died. Downgrade restores the schema, not the values.

`IF EXISTS` on all four so the migration is idempotent on a database an
operator already hand-pruned.

**Deploy order: restart first, then migrate** — the reverse of the repo
default in AGENTS.md, same reasoning as `f4a8b26c9d31` (#261): the *previous*
release still maps all four columns and SQLAlchemy names every mapped column
in its SELECTs, so migrating first fails every Domain query with
`UndefinedColumn` until the restart lands. Restart-first has no equivalent
window: the new code never references them, and all four carry server defaults
or are nullable, so the old schema accepts the new code's INSERTs. Recorded in
`docs/MIGRATIONS.md` → *Restart-before-migrate — one-time, `10783d8a2405`*.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "10783d8a2405"
down_revision: str | Sequence[str] | None = "d4e28a71c630"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the four columns the retired in-process rate limiter left behind."""
    op.execute("ALTER TABLE domains DROP COLUMN IF EXISTS max_concurrency")
    op.execute("ALTER TABLE domains DROP COLUMN IF EXISTS decay_window")
    op.execute("ALTER TABLE domains DROP COLUMN IF EXISTS current_interval")
    op.execute("ALTER TABLE domains DROP COLUMN IF EXISTS last_request_at")


def downgrade() -> None:
    """Recreate the columns at their genesis defaults, values unrecoverable.

    The dropped values exist nowhere else and were meaningless (see module
    docstring); recreating with the genesis server defaults satisfies the
    NOT NULL constraints and restores the schema `2addddea0b03` defined.
    """
    op.execute(
        "ALTER TABLE domains ADD COLUMN IF NOT EXISTS max_concurrency "
        "INTEGER NOT NULL DEFAULT 2"
    )
    op.execute(
        "ALTER TABLE domains ADD COLUMN IF NOT EXISTS decay_window "
        "FLOAT NOT NULL DEFAULT 1800.0"
    )
    op.execute(
        "ALTER TABLE domains ADD COLUMN IF NOT EXISTS current_interval "
        "FLOAT NOT NULL DEFAULT 1.0"
    )
    op.execute(
        "ALTER TABLE domains ADD COLUMN IF NOT EXISTS last_request_at "
        "TIMESTAMP WITH TIME ZONE"
    )
