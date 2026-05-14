"""drop snapshots + snapshot_chunks tables

Revision ID: a0f5414820b7
Revises: 6a1e7358a673
Create Date: 2026-05-14 02:09:59.103715

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0f5414820b7"
down_revision: Union[str, Sequence[str], None] = "6a1e7358a673"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop snapshot_chunks then snapshots.

    Phase 5 cutover (#156): Snapshot/SnapshotChunk-based pipeline removed.
    snapshot_chunks has a FK to snapshots so it must be dropped first.
    """
    op.drop_table("snapshot_chunks")
    op.drop_table("snapshots")


def downgrade() -> None:
    """One-way Phase 5 cutover — downgrade not supported.

    Restore from backup or replay migrations from the previous revision.
    """
    raise NotImplementedError("Phase 5 cutover is one-way; restore from backup or replay migrations.")
