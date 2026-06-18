"""#198 normalize JSONB 'null' literals to SQL NULL on optional JSONB columns.

Optional JSONB columns were written via SQLAlchemy with ``none_as_null`` at its
default (``False``), so a Python ``None`` persisted as the JSONB ``'null'``
literal rather than SQL ``NULL``. Both deserialize back to ``None``, so runtime
behavior was unaffected, but ``WHERE <col> IS NULL`` silently missed those rows
(operator queries, dashboard filters, future migrations).

The models now set ``none_as_null=True`` so new writes land SQL NULL. This data
migration normalizes the existing rows across all four affected columns. Each
UPDATE is idempotent (re-running matches nothing).

Downgrade is intentionally a no-op: once normalized to SQL NULL there is no way
to distinguish rows that were originally JSONB ``'null'`` from genuinely-unset
rows, and the JSONB ``'null'`` representation is exactly the inconsistency this
migration removes.

Revision ID: c7d8e9f0a1b2
Revises: 05b069981300
Create Date: 2026-06-18

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "05b069981300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, column) pairs holding optional JSONB that could carry the 'null' literal.
_NORMALIZE: tuple[tuple[str, str], ...] = (
    ("watched_items", "default_schedule_config"),
    ("notification_templates", "content_config"),
    ("watch_notification_configs", "content_config"),
    ("watched_item_notification_templates", "content_config"),
)


def upgrade() -> None:
    for table, column in _NORMALIZE:
        op.execute(
            f"UPDATE {table} SET {column} = NULL WHERE {column} = 'null'::jsonb"  # noqa: S608
        )


def downgrade() -> None:
    # No-op: SQL NULL is the canonical 'unset' representation; the JSONB 'null'
    # literal this migration removed cannot be reconstructed.
    pass
