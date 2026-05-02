"""add remote_channel_id to notification tables

Revision ID: 96641996744b
Revises: dc573e4c1328
Create Date: 2026-05-02 17:11:01.924418

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '96641996744b'
down_revision: Union[str, Sequence[str], None] = 'dc573e4c1328'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add remote_channel_id to notification_templates and watch_notification_configs.

    Nullable — backfilled by scripts/migrate_channels_to_notifier.py before
    USE_REMOTE_NOTIFY is enabled.
    """
    op.add_column('notification_templates', sa.Column('remote_channel_id', sa.String(length=26), nullable=True))
    op.add_column('watch_notification_configs', sa.Column('remote_channel_id', sa.String(length=26), nullable=True))


def downgrade() -> None:
    """Drop remote_channel_id columns."""
    op.drop_column('watch_notification_configs', 'remote_channel_id')
    op.drop_column('notification_templates', 'remote_channel_id')
