"""create pending_source_revisions outbox

Revision ID: 7474a83e42fd
Revises: 6bee3582aedc
Create Date: 2026-05-13 19:53:09.240646

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7474a83e42fd"
down_revision: str | Sequence[str] | None = "6bee3582aedc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "pending_source_revisions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("info_source_id", sa.String(length=26), nullable=False),
        sa.Column("content_fingerprint", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_media_type", sa.Text(), nullable=True),
        sa.Column("content_cache_uri", sa.Text(), nullable=False),
        sa.Column("content_cache_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "info_source_id",
            "content_fingerprint",
            name="uq_pending_source_revisions_source_fingerprint",
        ),
    )
    op.create_index(
        "ix_pending_source_revisions_info_source_id",
        "pending_source_revisions",
        ["info_source_id"],
        unique=False,
    )
    op.create_index(
        "ix_pending_source_revisions_next_attempt",
        "pending_source_revisions",
        ["next_attempt_at"],
        postgresql_where=sa.text("attempts < 10"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_pending_source_revisions_next_attempt",
        table_name="pending_source_revisions",
    )
    op.drop_index(
        "ix_pending_source_revisions_info_source_id",
        table_name="pending_source_revisions",
    )
    op.drop_table("pending_source_revisions")
