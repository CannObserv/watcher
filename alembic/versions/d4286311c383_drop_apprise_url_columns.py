"""drop apprise_url columns

Revision ID: d4286311c383
Revises: 0a55a6808cd4
Create Date: 2026-05-07 21:00:32.034349

Phase 5 of #137 — strip the local Apprise dispatch path. The notifier
service has been the only delivery path since Phase 4 (USE_REMOTE_NOTIFY=1)
and a Phase 4 backfill populated `remote_channel_id` on every active row.
Dropping the now-dead `apprise_url` columns and removing the dispatcher,
crypto helpers, and apprise dependency closes the cycle.

Pre-merge verification (greg, 2026-05-07):
    SELECT count(*) FROM watch_notification_configs
        WHERE remote_channel_id IS NULL AND is_active;  -- 0
    SELECT count(*) FROM notification_templates
        WHERE remote_channel_id IS NULL;                -- 0
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4286311c383"
down_revision: Union[str, Sequence[str], None] = "0a55a6808cd4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop apprise_url from notification_templates and watch_notification_configs.

    WARNING: irreversible in practice. The downgrade restores the column
    shape but NOT the Fernet ciphertext data, and the apprise dispatcher
    (src/core/notifications/dispatcher.py) was deleted in the same Phase 5
    sweep — there is no in-tree consumer left to read the column even if
    its data could be restored.
    """
    op.drop_column("notification_templates", "apprise_url")
    op.drop_column("watch_notification_configs", "apprise_url")


def downgrade() -> None:
    """Schema-only undo: restore the column shape but NOT the data.

    Re-added as nullable because the original Fernet ciphertext is
    irrecoverable once dropped — restoring NOT NULL would require a
    rebuild from external state we don't track. This downgrade exists
    for alembic-graph correctness only; the system is not functional in
    its pre-upgrade form after running it (the dispatcher and crypto
    helpers are gone from the codebase too).
    """
    op.add_column(
        "watch_notification_configs",
        sa.Column("apprise_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "notification_templates",
        sa.Column("apprise_url", sa.Text(), nullable=True),
    )
