"""add domain_name and domain_suspended to watched_items drop effective_domain from watches

Revision ID: db488115530a
Revises: b1c3e7a92d04
Create Date: 2026-05-27 04:35:12.409065

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db488115530a"
down_revision: str | Sequence[str] | None = "b1c3e7a92d04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add domain_name + domain_suspended to watched_items; backfill from watches; drop watches.effective_domain."""
    op.add_column(
        "watched_items",
        sa.Column("domain_name", sa.String(length=253), nullable=True),
    )
    op.add_column(
        "watched_items",
        sa.Column(
            "domain_suspended", sa.Boolean(), server_default="false", nullable=False
        ),
    )
    op.create_index(
        op.f("ix_watched_items_domain_name"), "watched_items", ["domain_name"], unique=False
    )
    op.create_foreign_key(
        "fk_watched_items_domain_name",
        "watched_items",
        "domains",
        ["domain_name"],
        ["name"],
        ondelete="SET NULL",
    )

    # Backfill domain_name from the earliest Watch per WatchedItem.
    op.execute(
        """
        UPDATE watched_items wi
        SET domain_name = w.effective_domain
        FROM (
            SELECT DISTINCT ON (watched_item_id)
                watched_item_id,
                effective_domain
            FROM watches
            WHERE effective_domain IS NOT NULL
            ORDER BY watched_item_id, created_at
        ) w
        WHERE wi.id = w.watched_item_id
        """
    )

    op.drop_column("watches", "effective_domain")


def downgrade() -> None:
    """Restore watches.effective_domain; remove watched_items domain columns."""
    op.add_column(
        "watches",
        sa.Column("effective_domain", sa.VARCHAR(length=253), autoincrement=False, nullable=True),
    )
    # Backfill effective_domain from watched_items.domain_name so the column is
    # not left entirely NULL after rollback.
    op.execute(
        """
        UPDATE watches w
        SET effective_domain = wi.domain_name
        FROM watched_items wi
        WHERE wi.id = w.watched_item_id
          AND wi.domain_name IS NOT NULL
        """
    )
    op.drop_constraint("fk_watched_items_domain_name", "watched_items", type_="foreignkey")
    op.drop_index(op.f("ix_watched_items_domain_name"), table_name="watched_items")
    op.drop_column("watched_items", "domain_suspended")
    op.drop_column("watched_items", "domain_name")
