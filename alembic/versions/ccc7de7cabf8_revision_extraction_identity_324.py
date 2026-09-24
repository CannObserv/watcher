"""record which extraction produced a revision (#324)

Revision ID: ccc7de7cabf8
Revises: 2f8bb8f7100a
Create Date: 2026-09-24

Two nullable ``TEXT`` columns on ``change_revisions``: ``spec_fingerprint``
(co-core's derivation over the source_spec the fallback loop actually bound,
cannobserv#309) and ``processor_version`` (the extraction's identity, co-core
version + local generation, cannobserv#486). Both were already computed per
revision and discarded with the outbox row the drain deletes on publish.

The diff design (``docs/plans/2026-09-24-observo-extraction-and-diff-design.md``,
Option A) reads them off the previous and current revisions to tell a spec- or
processor-induced fingerprint move from a content change. **Nullable is the
contract, not a convenience**: a NULL on a row written before this landed means
*unknown*, and the policy must treat unknown as "neither label nor re-baseline"
— so no backfill, and no default that would fabricate an identity for the 59
existing rows.

No deploy-order constraint: the ORM writes both columns on insert and reads
them nowhere yet, so the standard upgrade-then-restart order is safe (a
restarted process before the upgrade would fail its inserts; the usual order
avoids that).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ccc7de7cabf8"
down_revision: str | Sequence[str] | None = "2f8bb8f7100a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two extraction-identity columns, nullable, no default."""
    op.add_column("change_revisions", sa.Column("spec_fingerprint", sa.Text(), nullable=True))
    op.add_column("change_revisions", sa.Column("processor_version", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the two columns."""
    op.drop_column("change_revisions", "processor_version")
    op.drop_column("change_revisions", "spec_fingerprint")
