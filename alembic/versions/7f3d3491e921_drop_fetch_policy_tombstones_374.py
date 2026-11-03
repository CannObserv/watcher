"""drop fetch_policy_tombstones (#374)

Revision ID: 7f3d3491e921
Revises: 5580ec57b7aa
Create Date: 2026-11-03

F1 (#374) handed ``content.fetch-policy`` to Archiver. Watcher's producer
stopped at 2026-10-26T18:57:22Z (``4dea3b7``), and part 2 deletes it with the
``FetchPolicyTombstone`` model. The table carried a deleted host's
``revoked=True`` through every full-set republish (#245); nothing has
republished it since F1, so its rows are dead. The nightly backup holds them.
Downgrade restores the schema ``c398e0917f16`` created, empty.

``IF EXISTS`` so the migration is idempotent on a database an operator already
hand-pruned. No grants step: ``watcher`` owns the schema, and a table it
recreates on downgrade is covered by its default privileges (#259).

**Deploy order: restart first, then migrate** — the reverse of the repo
default in AGENTS.md, same reasoning as ``10783d8a2405`` (#272): the
*previous* release still maps the table and writes it on every domain create
and delete, so dropping first fails those paths until the restart lands.
Restart-first has no window: the new code never names the table, and nothing
reads it since F1. Recorded in `docs/MIGRATIONS.md` → *Restart-before-migrate —
one-time, `7f3d3491e921`*.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f3d3491e921"
down_revision: str | Sequence[str] | None = "5580ec57b7aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the fetch-policy tombstone table Watcher's retired producer kept."""
    op.execute("DROP TABLE IF EXISTS fetch_policy_tombstones")


def downgrade() -> None:
    """Recreate the table as ``c398e0917f16`` defined it, with no rows."""
    op.create_table(
        "fetch_policy_tombstones",
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("host"),
    )
