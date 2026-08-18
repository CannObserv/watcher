"""fetch_commands.forced_full_fetch (#269 CR-1)

Revision ID: d4e28a71c630
Revises: c1d93f4a5b27
Create Date: 2026-08-18 19:00:00.000000

Whether the occasion was asked for as an unconditional re-read — the operator's
check-now. Lineage rather than a request option: the reaper re-issues a stalled
command under a fresh ``command_id``, and without carrying this the forced intent
was lost, so the replacement could be answered 304 and produce no bytes at all —
the one thing check-now promises not to do.

NOT NULL with a ``false`` server default: every historical row predates the flag,
and none of them was forced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e28a71c630"
down_revision: str | None = "c1d93f4a5b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fetch_commands",
        sa.Column(
            "forced_full_fetch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("fetch_commands", "forced_full_fetch")
