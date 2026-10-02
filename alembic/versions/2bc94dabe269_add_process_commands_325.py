"""process_commands (#325)

Revision ID: 2bc94dabe269
Revises: b7e1d0c4a936
Create Date: 2026-10-02

The issuer's outbox / pending map / inbox for ``content.process``: one row per
(blob, spec) occasion, keyed on ``command_id``. Its shape and the issuer
discipline are ``fetch_commands``'s; see ``src/core/models/process_command.py``.

A new table, so no ordering hazard: the old code never names it, and the new
code only writes it under ``WATCHER_EXTRACT_MODE=shadow``. Standard order —
upgrade, then restart.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2bc94dabe269"
down_revision: str | None = "b7e1d0c4a936"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``process_commands`` and its three indexes."""
    op.create_table(
        "process_commands",
        sa.Column("command_id", sa.String(length=26), nullable=False),
        sa.Column("intent_id", sa.String(length=26), nullable=False),
        sa.Column("fetch_command_id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("info_source_id", sa.String(length=26), nullable=False),
        sa.Column("input_uri", sa.Text(), nullable=False),
        sa.Column("input_digest", sa.Text(), nullable=False),
        sa.Column("spec_index", sa.Integer(), nullable=False),
        sa.Column("source_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reissue_count", sa.Integer(), nullable=False),
        sa.Column("fact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("empty", sa.Boolean(), nullable=True),
        sa.Column("output_digest", sa.Text(), nullable=True),
        sa.Column("output_uri", sa.Text(), nullable=True),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("output_media_type", sa.Text(), nullable=True),
        sa.Column("spec_fingerprint", sa.Text(), nullable=True),
        sa.Column("spec_schema_version", sa.Integer(), nullable=True),
        sa.Column("processor_version", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_outcome", sa.String(length=20), nullable=True),
        sa.Column("local_fingerprint", sa.Text(), nullable=True),
        sa.Column("local_spec_fingerprint", sa.Text(), nullable=True),
        sa.Column("shadow_verdict", sa.String(length=20), nullable=True),
        sa.Column("shadow_detail", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["fetch_command_id"], ["fetch_commands.command_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("command_id"),
    )
    op.create_index(
        "ix_process_commands_open",
        "process_commands",
        ["issued_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending_publish', 'in_flight')"),
    )
    op.create_index(
        "ix_process_commands_fetch_command_id",
        "process_commands",
        ["fetch_command_id"],
        unique=False,
    )
    op.create_index("ix_process_commands_fact_at", "process_commands", ["fact_at"], unique=False)


def downgrade() -> None:
    """Drop ``process_commands``."""
    op.drop_index("ix_process_commands_fact_at", table_name="process_commands")
    op.drop_index("ix_process_commands_fetch_command_id", table_name="process_commands")
    op.drop_index(
        "ix_process_commands_open",
        table_name="process_commands",
        postgresql_where=sa.text("status IN ('pending_publish', 'in_flight')"),
    )
    op.drop_table("process_commands")
