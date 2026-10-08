"""strip the five never-firing events from notification templates (#166)

Revision ID: 78606286a887
Revises: c3f9a1d27b84
Create Date: 2026-10-08

Data only — autogenerate found no schema diff. `WatchEventType` lost
`watch_created`, `watch_paused`, `watch_resumed`, `watch_archived` and
`watch_deleted`: each was subscribable and none had a dispatch site. This strips
them from every `notification_templates.events` array, and from
`content_config.overrides`, whose keys `ContentConfig` validates against the
enum — a stale key would fail every schema read of its row.

Order is preserved. A row left subscribed to nothing stays that way: it never
fired, and substituting `change_detected` would deliver notifications nobody
asked for. The values are literals, not the enum — they no longer exist there.

**Deploy order: the repo default, migrate then restart.** Old code against
migrated rows is safe (its enum is a superset). New code against unmigrated rows
renders, but cannot round-trip a stale override key.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "78606286a887"
down_revision: str | Sequence[str] | None = "c3f9a1d27b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED = ("watch_created", "watch_paused", "watch_resumed", "watch_archived", "watch_deleted")


def upgrade() -> None:
    """Remove each dropped value from `events` and from the override keys."""
    for value in _DROPPED:
        op.execute(
            sa.text(
                "UPDATE notification_templates SET events = array_remove(events, :value)"
                " WHERE :value = ANY(events)"
            ).bindparams(value=value)
        )
        op.execute(
            sa.text(
                "UPDATE notification_templates SET content_config = jsonb_set("
                "content_config, '{overrides}', (content_config -> 'overrides') - :value)"
                " WHERE content_config -> 'overrides' ? :value"
            ).bindparams(value=value)
        )


def downgrade() -> None:
    """No-op: the stripped values never fired, so nothing is lost by keeping
    them out, and restoring them would re-offer a subscription that cannot
    deliver. Which rows held them is not recorded."""
