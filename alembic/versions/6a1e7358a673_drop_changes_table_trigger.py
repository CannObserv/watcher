"""drop changes table + trigger

Revision ID: 6a1e7358a673
Revises: 96aba824f3f2
Create Date: 2026-05-14 01:25:03.213057

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a1e7358a673"
down_revision: Union[str, Sequence[str], None] = "96aba824f3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the changes table and its associated trigger/function.

    Phase 5 cutover (#156): Change/Snapshot-based diff pipeline removed.
    The trigger kept watches.last_changed_at in sync with changes rows;
    that column is now updated directly by the pipeline task.
    """
    op.execute("DROP TRIGGER IF EXISTS trg_changes_update_last_changed_at ON changes")
    op.execute("DROP FUNCTION IF EXISTS update_watch_last_changed_at()")
    op.drop_index("ix_changes_info_item_id", table_name="changes")
    op.drop_index("ix_changes_info_item_id_detected_at", table_name="changes")
    op.drop_index(
        "ix_changes_unpublished_detected_at",
        table_name="changes",
        postgresql_where="(published_to_bus_at IS NULL)",
    )
    op.drop_table("changes")


def downgrade() -> None:
    """One-way Phase 5 cutover — downgrade not supported.

    To restore, re-run the previous migration that created this table.
    """
    raise NotImplementedError("Phase 5 cutover is one-way; restore from backup or replay migrations.")
