"""Add cascade delete to watch and snapshot foreign keys.

Revision ID: a1b2c3d4e5f6
Revises: 71349ff5f740
Create Date: 2026-03-22
"""

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "71349ff5f740"
branch_labels = None
depends_on = None

# (table, constraint_name, column, referred_table, on_delete)
FK_CHANGES = [
    ("snapshots", "snapshots_watch_id_fkey", "watch_id", "watches", "CASCADE"),
    ("changes", "changes_watch_id_fkey", "watch_id", "watches", "CASCADE"),
    ("temporal_profiles", "temporal_profiles_watch_id_fkey", "watch_id", "watches", "CASCADE"),
    (
        "notification_configs",
        "notification_configs_watch_id_fkey",
        "watch_id",
        "watches",
        "CASCADE",
    ),
    ("audit_log", "audit_log_watch_id_fkey", "watch_id", "watches", "SET NULL"),
    ("snapshot_chunks", "snapshot_chunks_snapshot_id_fkey", "snapshot_id", "snapshots", "CASCADE"),
    (
        "changes",
        "changes_previous_snapshot_id_fkey",
        "previous_snapshot_id",
        "snapshots",
        "CASCADE",
    ),
    ("changes", "changes_current_snapshot_id_fkey", "current_snapshot_id", "snapshots", "CASCADE"),
]


def upgrade() -> None:
    """Drop and recreate foreign keys with ON DELETE cascade/set null."""
    for table, constraint, column, referred, on_delete in FK_CHANGES:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, referred, [column], ["id"], ondelete=on_delete)


def downgrade() -> None:
    """Restore original foreign keys with NO ACTION."""
    for table, constraint, column, referred, _on_delete in FK_CHANGES:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, referred, [column], ["id"])
