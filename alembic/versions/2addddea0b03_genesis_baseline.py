"""genesis baseline — squash of the pre-#234 migration chain

Revision ID: 2addddea0b03
Revises:
Create Date: 2026-07-25

This single migration materializes the entire Watcher public schema as the
SQLAlchemy models describe it, replacing the ~40-file linear chain that ran
from the project's inception through ``c5d6e7f8a9b0`` (#218).

Why squash (#234): the old chain transiently created a cross-schema FK into
the Archiver-owned ``information`` schema (``9e86f9e4d704``) and dropped it a
few revisions later (``b1c3e7a92d04``) once Archiver moved to its own database
(#149). Replaying the chain from base therefore required Archiver's schema to
be seeded first — a coupling that exists at no revision any real environment
lives at, and one that broke ``alembic upgrade head`` on a clean database. The
squash removes that coupling at the source: this baseline touches no
``information`` schema, so a from-empty ``upgrade head`` is fully self-contained
(no Archiver checkout, no cross-service ordering) and CI can smoke-check it
against a bare Postgres.

Faithfulness: this baseline is the models-truth schema. It intentionally omits
two pieces of dead cruft the old chain left in the database — the orphaned
``trg_fn_watches_last_changed_at()`` plpgsql function (its ``watches`` table was
dropped in #191) and the vestigial ``notification_event_types`` catalog table
(never queried at runtime; the codes live in the ``EventType`` enum). ``alembic
check`` is clean against a DB built from this file.

OPERATOR ACTION on already-migrated databases (prod + any dev DB): this file
replaces the old chain, so their ``alembic_version`` points at a revision that
no longer exists. At deploy, stamp them at this baseline instead of upgrading:

    uv run alembic stamp 2addddea0b03 --purge

Fresh databases need no stamp — they run this migration normally. See
``docs/MIGRATIONS.md`` → "Migration baseline (squash)".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2addddea0b03"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the full Watcher public schema."""
    op.create_table(
        "app_users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_log_created_at",
        "audit_log",
        [sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_log_event_type",
        "audit_log",
        ["event_type", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_log_payload_watched_item_id",
        "audit_log",
        [
            sa.literal_column("(payload->>'watched_item_id')"),
            sa.literal_column("created_at DESC"),
        ],
        unique=False,
    )
    op.create_table(
        "domains",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=253), nullable=False),
        sa.Column("min_interval", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("max_concurrency", sa.Integer(), server_default="2", nullable=False),
        sa.Column("current_interval", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("last_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decay_window", sa.Float(), server_default="1800.0", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "default_schedule_config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("key_prefix", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_table(
        "watched_items",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("archiver_info_item_id", sa.String(length=26), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "default_schedule_config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("content_media_type", sa.String(length=2048), nullable=True),
        sa.Column("default_tags", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("domain_name", sa.String(length=253), nullable=True),
        sa.Column("domain_suspended", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "domain_default_schedule_config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("effective_url", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "source_specs",
            postgresql.ARRAY(postgresql.JSONB(astext_type=sa.Text())),
            server_default=sa.text("ARRAY[]::jsonb[]"),
            nullable=False,
        ),
        sa.Column("archiver_info_source_id", sa.String(length=26), nullable=True),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "health_status",
            sa.String(length=10),
            server_default="unknown",
            nullable=True,
        ),
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
        sa.ForeignKeyConstraint(["domain_name"], ["domains.name"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_watched_items_archiver_info_item_id",
        "watched_items",
        ["archiver_info_item_id"],
        unique=True,
        postgresql_where=sa.text("archiver_info_item_id IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_watched_items_domain_name"),
        "watched_items",
        ["domain_name"],
        unique=False,
    )
    op.create_table(
        "change_revisions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("content_fingerprint", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("archiver_revision_id", sa.String(length=26), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_change_revisions_watched_item_id"),
        "change_revisions",
        ["watched_item_id"],
        unique=False,
    )
    op.create_table(
        "notification_templates",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("channel_hint", sa.String(length=50), nullable=False),
        sa.Column(
            "events",
            postgresql.ARRAY(sa.String(length=50)),
            server_default=sa.text("ARRAY['change_detected']::varchar[]"),
            nullable=False,
        ),
        sa.Column("visibility", sa.String(length=20), server_default="global", nullable=False),
        sa.Column("domain_name", sa.String(length=253), nullable=True),
        sa.Column("watched_item_id", sa.String(length=26), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "content_config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("remote_channel_id", sa.String(length=26), nullable=True),
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
        sa.CheckConstraint(
            "(visibility = 'global'"
            " AND domain_name IS NULL AND watched_item_id IS NULL)"
            " OR (visibility = 'domain'"
            " AND domain_name IS NOT NULL AND watched_item_id IS NULL)"
            " OR (visibility = 'watched_item'"
            " AND watched_item_id IS NOT NULL AND domain_name IS NULL)",
            name="ck_notification_templates_visibility_refs",
        ),
        sa.ForeignKeyConstraint(["domain_name"], ["domains.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_templates_domain_name"),
        "notification_templates",
        ["domain_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_templates_watched_item_id"),
        "notification_templates",
        ["watched_item_id"],
        unique=False,
    )
    op.create_table(
        "temporal_profiles",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("profile_type", sa.String(length=20), nullable=False),
        sa.Column("reference_date", sa.Date(), nullable=True),
        sa.Column("date_range_start", sa.Date(), nullable=True),
        sa.Column("date_range_end", sa.Date(), nullable=True),
        sa.Column(
            "rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("post_action", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
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
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("watched_item_id"),
    )
    op.create_table(
        "pending_archiver_sync",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("change_revision_id", sa.String(length=26), nullable=False),
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["change_revision_id"], ["change_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("change_revision_id"),
    )
    op.create_index(
        op.f("ix_pending_archiver_sync_watched_item_id"),
        "pending_archiver_sync",
        ["watched_item_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the full Watcher public schema."""
    op.drop_index(
        op.f("ix_pending_archiver_sync_watched_item_id"),
        table_name="pending_archiver_sync",
    )
    op.drop_table("pending_archiver_sync")
    op.drop_table("temporal_profiles")
    op.drop_index(
        op.f("ix_notification_templates_watched_item_id"),
        table_name="notification_templates",
    )
    op.drop_index(
        op.f("ix_notification_templates_domain_name"),
        table_name="notification_templates",
    )
    op.drop_table("notification_templates")
    op.drop_index(op.f("ix_change_revisions_watched_item_id"), table_name="change_revisions")
    op.drop_table("change_revisions")
    op.drop_index(op.f("ix_watched_items_domain_name"), table_name="watched_items")
    op.drop_index(
        "ix_watched_items_archiver_info_item_id",
        table_name="watched_items",
        postgresql_where=sa.text("archiver_info_item_id IS NOT NULL"),
    )
    op.drop_table("watched_items")
    op.drop_table("api_keys")
    op.drop_table("domains")
    op.drop_index("ix_audit_log_payload_watched_item_id", table_name="audit_log")
    op.drop_index("ix_audit_log_event_type", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("app_users")
