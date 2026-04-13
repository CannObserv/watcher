"""add watch_archived and watch_deleted event types

Revision ID: ef3076ce30aa
Revises: 07fe64d2fbc3
Create Date: 2026-04-13 17:08:33.868277

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ef3076ce30aa'
down_revision: Union[str, Sequence[str], None] = '07fe64d2fbc3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Insert watch_archived and watch_deleted into notification_event_types seed table."""
    op.execute("""
        INSERT INTO notification_event_types (code, label, description, is_active)
        VALUES
            ('watch_archived', 'Watch Archived', 'A watch was archived', true),
            ('watch_deleted', 'Watch Deleted', 'A watch was permanently deleted', true)
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    """Remove watch_archived and watch_deleted from notification_event_types."""
    op.execute("""
        DELETE FROM notification_event_types
        WHERE code IN ('watch_archived', 'watch_deleted')
    """)
