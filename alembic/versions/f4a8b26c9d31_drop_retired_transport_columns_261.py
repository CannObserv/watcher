"""drop the retired outbox and revision columns (#261)

Revision ID: f4a8b26c9d31
Revises: a9d40c7e1b52
Create Date: 2026-08-17

The **contract** half of the expand started in `32140463c26c`. That migration
released `pending_archiver_sync.content_cache_uri` /
`content_cache_expires_at` to nullable rather than dropping them, because no
single deploy order makes dropping a NOT NULL column safe: migrate-first breaks
the running old code (it still INSERTs them), restart-first breaks the new code
(it does not). The `content.revisions` publisher is live, so the contract is now
a no-risk drop — nothing has written either column since the cutover.

`change_revisions.archiver_revision_id` goes with them, and is different in
kind: it is dead but it **holds data**. The HTTP write path back-populated
Archiver's minted SourceRevision id so the cache sweeper could PATCH against it;
Archiver now allocates on its side of `content.revisions` and never reports
back, so nothing can populate it again. Dropping is safe because the ids are
redundant, not unique — Archiver identifies a SourceRevision by
`(info_source_id, content_fingerprint)`
(`uq_source_revisions_source_fingerprint`, the same pair its upsert conflicts
on), so the mapping is re-derivable from data Watcher still keeps.

`IF EXISTS` on all three so the migration is idempotent on a database an
operator already hand-pruned.

**Deploy order: restart first, then migrate** — the reverse of the repo default
in AGENTS.md, because the *previous* release still maps all three columns and
SQLAlchemy names every mapped column in its SELECTs. Migrating first drops them
out from under the running process and every query against these two tables
fails with `UndefinedColumn` until the restart lands. Restart-first has no
equivalent window: the new code never references them, and all three are
nullable, so nothing it writes needs them. Recorded in
`docs/MIGRATIONS.md` → *Restart-before-migrate — one-time, `f4a8b26c9d31`*.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a8b26c9d31"
down_revision: str | Sequence[str] | None = "a9d40c7e1b52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the two released scratch-cache columns and the dead registry id."""
    op.execute("ALTER TABLE pending_archiver_sync DROP COLUMN IF EXISTS content_cache_uri")
    op.execute("ALTER TABLE pending_archiver_sync DROP COLUMN IF EXISTS content_cache_expires_at")
    op.execute("ALTER TABLE change_revisions DROP COLUMN IF EXISTS archiver_revision_id")


def downgrade() -> None:
    """Recreate the columns, nullable and empty.

    The values are unrecoverable — no code has written any of the three since
    #253, and the `archiver_revision_id` values this drops exist nowhere else in
    Watcher. Recreating them nullable restores the schema `32140463c26c` left
    (both cache columns nullable), which is what a downgrade past this revision
    needs; that revision's own downgrade restores NOT NULL after deleting rows
    that lack the values.
    """
    op.execute(
        "ALTER TABLE change_revisions ADD COLUMN IF NOT EXISTS archiver_revision_id VARCHAR(26)"
    )
    op.execute(
        "ALTER TABLE pending_archiver_sync "
        "ADD COLUMN IF NOT EXISTS content_cache_expires_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE pending_archiver_sync ADD COLUMN IF NOT EXISTS content_cache_uri TEXT")
