"""drop the headless jump's retired tables and columns (#380)

Revision ID: c8e1d4f7a2b6
Revises: e2eab993d615
Create Date: 2026-11-10

The headless jump (#368) handed Watcher's operator surface to Archiver:
#377 deleted the dashboard, #378 ``/api/v1`` and its keys, #379 the
notification subsystem, F2 (#375) the domain cadence tier and the temporal
profiles. What those surfaces alone wrote goes here: four tables, eight
``watched_items`` columns and four ``domains`` columns. #374 and #376
already dropped ``fetch_policy_tombstones`` and ``last_error_notified_at``.

Production on 2026-11-10, read before writing this: no archived item and no
archived domain, so dropping ``archived_at`` lifts no pause or suspension,
and no item in ``probing``. So this is a plain drop, with no back-fill. The
nightly backup holds the values.

``IF EXISTS`` so the migration is idempotent on a database an operator
already hand-pruned. ``api_keys`` goes before ``app_users``, which it
references. No grants step: ``watcher`` owns every table here (#259).

**Deploy order: stop, migrate, start**, not restart-first.
``watched_items.name`` is NOT NULL with no server default, so the new code's
reconcile INSERTs, which no longer name it, would fail against the old schema
until the drop landed. Stopping first leaves no window. Recorded in
`docs/MIGRATIONS-HISTORY.md` → *Stop-migrate-start — one-time, `c8e1d4f7a2b6`*.

Downgrade restores the genesis (``2addddea0b03``) and ``b3f1a7c50d94`` shapes,
empty, with one exception. ``watched_items.name`` comes back **nullable**:
genesis made it NOT NULL with no default, and re-adding that to a table with
rows fails. The previous release always writes a name, so new rows are whole.
Nothing back-fills the old ones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e1d4f7a2b6"
down_revision: str | Sequence[str] | None = "e2eab993d615"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Referencing tables first: api_keys → app_users; both FKs onto the kept tables
# drop with their own tables.
_TABLES = ("notification_templates", "temporal_profiles", "api_keys", "app_users")
_WATCHED_ITEM_COLUMNS = (
    "name",
    "description",
    "default_tags",
    "default_schedule_config",
    "domain_default_schedule_config",
    "throttle_floor_interval",
    "last_reviewed_at",
    "archived_at",
)
_DOMAIN_COLUMNS = ("min_interval", "default_schedule_config", "notes", "archived_at")


def upgrade() -> None:
    """Drop what only the retired operator surface wrote."""
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    for column in _WATCHED_ITEM_COLUMNS:
        op.execute(f"ALTER TABLE watched_items DROP COLUMN IF EXISTS {column}")
    for column in _DOMAIN_COLUMNS:
        op.execute(f"ALTER TABLE domains DROP COLUMN IF EXISTS {column}")


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def downgrade() -> None:
    """Restore the shapes ``2addddea0b03`` and ``b3f1a7c50d94`` created, empty."""
    op.add_column("domains", sa.Column("min_interval", sa.Float(), server_default="1.0", nullable=False))
    op.add_column("domains", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column(
        "domains",
        sa.Column(
            "default_schedule_config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("domains", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))

    # Nullable, unlike genesis: see the module docstring.
    op.add_column("watched_items", sa.Column("name", sa.String(length=255), nullable=True))
    op.add_column("watched_items", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "watched_items", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "watched_items", sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "watched_items",
        sa.Column(
            "default_schedule_config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("watched_items", sa.Column("default_tags", postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column(
        "watched_items",
        sa.Column(
            "domain_default_schedule_config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "watched_items", sa.Column("throttle_floor_interval", sa.String(length=16), nullable=True)
    )

    op.create_table(
        "app_users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
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
        *_timestamps(),
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
        "ix_notification_templates_domain_name",
        "notification_templates",
        ["domain_name"],
        unique=False,
    )
    op.create_index(
        "ix_notification_templates_watched_item_id",
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
        *_timestamps(),
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("watched_item_id"),
    )
