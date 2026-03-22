"""add cascade delete to watch and snapshot foreign keys

Revision ID: f0083c076bea
Revises: 71349ff5f740
Create Date: 2026-03-22

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f0083c076bea'
down_revision: str | Sequence[str] | None = '71349ff5f740'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, constraint_name, column, referred_table, on_delete)
FK_CHANGES = [
    ('snapshots', 'snapshots_watch_id_fkey', 'watch_id', 'watches', 'CASCADE'),
    ('changes', 'changes_watch_id_fkey', 'watch_id', 'watches', 'CASCADE'),
    (
        'temporal_profiles',
        'temporal_profiles_watch_id_fkey',
        'watch_id',
        'watches',
        'CASCADE',
    ),
    (
        'notification_configs',
        'notification_configs_watch_id_fkey',
        'watch_id',
        'watches',
        'CASCADE',
    ),
    ('audit_log', 'audit_log_watch_id_fkey', 'watch_id', 'watches', 'SET NULL'),
    (
        'snapshot_chunks',
        'snapshot_chunks_snapshot_id_fkey',
        'snapshot_id',
        'snapshots',
        'CASCADE',
    ),
    (
        'changes',
        'changes_previous_snapshot_id_fkey',
        'previous_snapshot_id',
        'snapshots',
        'CASCADE',
    ),
    (
        'changes',
        'changes_current_snapshot_id_fkey',
        'current_snapshot_id',
        'snapshots',
        'CASCADE',
    ),
]


def upgrade() -> None:
    """Drop and recreate foreign keys with ON DELETE cascade/set null."""
    for table, constraint, column, referred, on_delete in FK_CHANGES:
        op.drop_constraint(constraint, table, type_='foreignkey')
        op.create_foreign_key(
            constraint, table, referred, [column], ['id'], ondelete=on_delete,
        )


def downgrade() -> None:
    """Restore original foreign keys with NO ACTION."""
    for table, constraint, column, referred, _on_delete in FK_CHANGES:
        op.drop_constraint(constraint, table, type_='foreignkey')
        op.create_foreign_key(constraint, table, referred, [column], ['id'])
