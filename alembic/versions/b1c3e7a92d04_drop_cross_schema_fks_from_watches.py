"""drop cross-schema FKs from watches (archiver is a separate DB)

Revision ID: b1c3e7a92d04
Revises: fc32c9a4bc9e
Create Date: 2026-05-22 23:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c3e7a92d04"
down_revision: str | Sequence[str] | None = "fc32c9a4bc9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the cross-schema FK constraints from watches.info_item_id and
    watches.target_info_source_id.

    Archiver runs against a separate PostgreSQL database (``archiver`` DB),
    so information.info_items in the watcher DB is a permanently-empty stub.
    The FK constraints can never be satisfied in production and block all
    Watch creation. Validation is enforced at the application layer via
    fetch_info_item_bindings (ArchiverClient HTTP call) before any insert,
    mirroring WatchedItem.info_item_id which deliberately carries no FK.
    """
    op.drop_constraint("fk_watches_info_item_id", "watches", type_="foreignkey")
    op.drop_constraint("fk_watches_target_info_source_id", "watches", type_="foreignkey")


def downgrade() -> None:
    """Restore cross-schema FK constraints (only valid when sharing a DB with Archiver)."""
    op.execute("CREATE SCHEMA IF NOT EXISTS information")
    op.execute(
        "CREATE TABLE IF NOT EXISTS information.info_sources"
        " (info_source_id varchar(26) PRIMARY KEY)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS information.info_items"
        " (info_item_id varchar(26) PRIMARY KEY)"
    )
    op.create_foreign_key(
        "fk_watches_target_info_source_id",
        "watches",
        "info_sources",
        ["target_info_source_id"],
        ["info_source_id"],
        referent_schema="information",
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_watches_info_item_id",
        "watches",
        "info_items",
        ["info_item_id"],
        ["info_item_id"],
        referent_schema="information",
        ondelete="RESTRICT",
    )
