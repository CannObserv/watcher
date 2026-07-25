"""drop post-squash cruft: orphaned trigger fn + vestigial notification_event_types

Revision ID: b1f7c0a2d3e4
Revises: 2addddea0b03
Create Date: 2026-07-25

Converges already-migrated databases onto the #234 genesis baseline by dropping
two dead objects the pre-squash chain left behind (#235). The baseline
(`2addddea0b03`) never created them; this migration removes them from databases
that were stamped onto the baseline rather than built fresh.

- ``trg_fn_watches_last_changed_at()`` — an orphaned plpgsql trigger function
  whose body targets the ``watches`` table, dropped in #191. No trigger
  references it (the trigger went with the table).
- ``notification_event_types`` — a catalog table never queried at runtime and
  not a FK target. It was superseded by the ``WatchEventType`` enum
  (``src/core/notifications/events.py``), which is the authoritative source of
  event-type codes; the table was left unread. (Its seed still mirrored the live
  enum — it was unused, not stale.)

Everything uses ``IF EXISTS`` so this is a no-op on fresh installs (which never
had either object) and idempotent on already-migrated databases. Note there is
deliberately **no** ``DROP TRIGGER … ON watches`` — the ``watches`` table is
gone, and Postgres errors on the missing relation even with ``IF EXISTS``; the
trigger was already removed when the table was dropped.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1f7c0a2d3e4"
down_revision: str | Sequence[str] | None = "2addddea0b03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the orphaned trigger function and the vestigial catalog table."""
    op.execute("DROP FUNCTION IF EXISTS trg_fn_watches_last_changed_at()")
    op.execute("DROP TABLE IF EXISTS notification_event_types")


def downgrade() -> None:
    """No-op — these objects are dead cruft; recreating them serves no purpose."""
