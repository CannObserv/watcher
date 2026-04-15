"""add trigger: update watches.last_changed_at on change insert

Revision ID: ecf003d279f1
Revises: 917d378da82f
Create Date: 2026-04-15 13:07:07.650467

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ecf003d279f1"
down_revision: Union[str, Sequence[str], None] = "917d378da82f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create trigger that stamps watches.last_changed_at on every Change INSERT."""
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_fn_watches_last_changed_at()
        RETURNS TRIGGER AS $$
        BEGIN
            UPDATE watches
               SET last_changed_at = NEW.detected_at
             WHERE id = NEW.watch_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_changes_update_last_changed_at
        AFTER INSERT ON changes
        FOR EACH ROW
        EXECUTE FUNCTION trg_fn_watches_last_changed_at();
    """)


def downgrade() -> None:
    """Drop trigger and supporting function."""
    op.execute("DROP TRIGGER IF EXISTS trg_changes_update_last_changed_at ON changes;")
    op.execute("DROP FUNCTION IF EXISTS trg_fn_watches_last_changed_at();")
