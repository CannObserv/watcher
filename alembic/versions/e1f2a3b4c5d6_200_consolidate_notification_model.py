"""Consolidate notification model onto one scoped table — #200.

Collapses the five dispatch sources into ``notification_templates`` with an
intrinsic ``visibility`` (global / domain / watched_item). Backfills globals,
domain/watch refs, and the two inline item tables into scoped rows (cloning a
template attached at multiple scopes), drops orphan unreferenced templates,
then drops ``is_global_default`` and the four legacy tables.

Data-preserving and reversible. On the live DB the four legacy tables are
empty and only global templates exist, so the data moves are near no-ops; the
loops below cover the general case for completeness.

Revision ID: e1f2a3b4c5d6
Revises: c7d8e9f0a1b2
Create Date: 2026-06-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from ulid import ULID

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIS_CHECK = (
    "(visibility = 'global' AND domain_name IS NULL AND watched_item_id IS NULL) OR "
    "(visibility = 'domain' AND domain_name IS NOT NULL AND watched_item_id IS NULL) OR "
    "(visibility = 'watched_item' AND watched_item_id IS NOT NULL AND domain_name IS NULL)"
)


def _clone_scoped(conn, template_id, *, visibility, domain_name=None, watched_item_id=None) -> None:
    """Insert a copy of *template_id* with a new scope (INSERT...SELECT preserves types)."""
    conn.execute(
        sa.text(
            "INSERT INTO notification_templates "
            "(id, title, channel_hint, events, is_active, content_config, remote_channel_id, "
            " visibility, domain_name, watched_item_id, created_at, updated_at) "
            "SELECT :nid, title, channel_hint, events, is_active, content_config, "
            "remote_channel_id, :vis, :dn, :wid, now(), now() "
            "FROM notification_templates WHERE id = :tid"
        ),
        {
            "nid": str(ULID()),
            "vis": visibility,
            "dn": domain_name,
            "wid": watched_item_id,
            "tid": template_id,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()

    # 1. New columns (visibility nullable during backfill, tightened at the end).
    op.add_column("notification_templates", sa.Column("visibility", sa.String(length=20)))
    op.add_column("notification_templates", sa.Column("domain_name", sa.String(length=253)))
    op.add_column("notification_templates", sa.Column("watched_item_id", sa.String(length=26)))

    # 2. Globals.
    conn.execute(
        sa.text("UPDATE notification_templates SET visibility = 'global' WHERE is_global_default")
    )

    # 3. Domain refs — first attachment claims the row, extras clone.
    for dn, tid in conn.execute(
        sa.text("SELECT domain_name, template_id FROM domain_nc_refs")
    ).fetchall():
        claimed = conn.execute(
            sa.text("SELECT visibility FROM notification_templates WHERE id = :t"), {"t": tid}
        ).scalar()
        if claimed is None:
            conn.execute(
                sa.text(
                    "UPDATE notification_templates SET visibility='domain', domain_name=:d "
                    "WHERE id=:t"
                ),
                {"d": dn, "t": tid},
            )
        else:
            _clone_scoped(conn, tid, visibility="domain", domain_name=dn)

    # 4. Watched-item refs.
    for wid, tid in conn.execute(
        sa.text("SELECT watched_item_id, template_id FROM watch_nc_refs")
    ).fetchall():
        claimed = conn.execute(
            sa.text("SELECT visibility FROM notification_templates WHERE id = :t"), {"t": tid}
        ).scalar()
        if claimed is None:
            conn.execute(
                sa.text(
                    "UPDATE notification_templates SET visibility='watched_item', "
                    "watched_item_id=:w WHERE id=:t"
                ),
                {"w": wid, "t": tid},
            )
        else:
            _clone_scoped(conn, tid, visibility="watched_item", watched_item_id=wid)

    # 5. Copy the two inline item tables into scoped rows (title is required now).
    for src in ("watched_item_notification_templates", "watch_notification_configs"):
        ids = [
            row[0] for row in conn.execute(sa.text(f"SELECT id FROM {src}")).fetchall()  # noqa: S608
        ]
        for sid in ids:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_templates "
                    "(id, title, channel_hint, events, is_active, content_config, "
                    " remote_channel_id, visibility, watched_item_id, created_at, updated_at) "
                    "SELECT :nid, COALESCE(title, channel_hint, 'Untitled'), channel_hint, "
                    "events, is_active, content_config, remote_channel_id, 'watched_item', "
                    f"watched_item_id, now(), now() FROM {src} WHERE id = :sid"  # noqa: S608
                ),
                {"nid": str(ULID()), "sid": sid},
            )

    # 6. Drop orphan templates: non-global, unreferenced (visibility never set).
    orphans = conn.execute(
        sa.text("SELECT count(*) FROM notification_templates WHERE visibility IS NULL")
    ).scalar()
    if orphans:
        print(f"[#200 migration] dropping {orphans} orphan notification template(s)")  # noqa: T201
        conn.execute(sa.text("DELETE FROM notification_templates WHERE visibility IS NULL"))

    # 7. Tighten the new columns + add the consistency CHECK, FKs, indexes.
    op.alter_column(
        "notification_templates",
        "visibility",
        nullable=False,
        server_default="global",
    )
    op.create_check_constraint(
        "ck_notification_templates_visibility_refs", "notification_templates", _VIS_CHECK
    )
    op.create_foreign_key(
        "notification_templates_domain_name_fkey",
        "notification_templates",
        "domains",
        ["domain_name"],
        ["name"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "notification_templates_watched_item_id_fkey",
        "notification_templates",
        "watched_items",
        ["watched_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_notification_templates_domain_name", "notification_templates", ["domain_name"]
    )
    op.create_index(
        "ix_notification_templates_watched_item_id", "notification_templates", ["watched_item_id"]
    )

    # 8. Drop the legacy flag + tables.
    op.drop_column("notification_templates", "is_global_default")
    op.drop_table("watch_nc_refs")
    op.drop_table("domain_nc_refs")
    op.drop_table("watched_item_notification_templates")
    op.drop_table("watch_notification_configs")


def _recreate_legacy_tables() -> None:
    """Recreate the four legacy tables (for downgrade)."""
    op.create_table(
        "domain_nc_refs",
        sa.Column("domain_name", sa.String(length=253), nullable=False),
        sa.Column("template_id", sa.String(length=26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["domain_name"], ["domains.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["notification_templates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("domain_name", "template_id"),
    )
    op.create_table(
        "watch_nc_refs",
        sa.Column("watched_item_id", sa.String(length=26), nullable=False),
        sa.Column("template_id", sa.String(length=26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["notification_templates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("watched_item_id", "template_id"),
    )
    for tbl in ("watched_item_notification_templates", "watch_notification_configs"):
        op.create_table(
            tbl,
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("watched_item_id", sa.String(length=26), nullable=False),
            sa.Column("title", sa.String(length=100), nullable=True),
            sa.Column("channel_hint", sa.String(length=50), nullable=False),
            sa.Column("events", sa.ARRAY(sa.String(length=50)), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
            sa.Column("content_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("remote_channel_id", sa.String(length=26), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    _recreate_legacy_tables()

    # Restore the is_global_default flag.
    op.add_column(
        "notification_templates",
        sa.Column("is_global_default", sa.Boolean(), server_default="false", nullable=False),
    )
    conn.execute(
        sa.text("UPDATE notification_templates SET is_global_default = true WHERE visibility='global'")
    )

    # Re-split scoped rows back into the legacy structures, then remove them from the library.
    for dn, tid in conn.execute(
        sa.text(
            "SELECT domain_name, id FROM notification_templates WHERE visibility='domain'"
        )
    ).fetchall():
        conn.execute(
            sa.text(
                "INSERT INTO domain_nc_refs (domain_name, template_id) VALUES (:d, :t)"
            ),
            {"d": dn, "t": tid},
        )
    for sid, wid, title, hint in conn.execute(
        sa.text(
            "SELECT id, watched_item_id, title, channel_hint FROM notification_templates "
            "WHERE visibility='watched_item'"
        )
    ).fetchall():
        conn.execute(
            sa.text(
                "INSERT INTO watched_item_notification_templates "
                "(id, watched_item_id, title, channel_hint, events, is_active, content_config, "
                " remote_channel_id, created_at, updated_at) "
                "SELECT :nid, watched_item_id, title, channel_hint, events, is_active, "
                "content_config, remote_channel_id, created_at, updated_at "
                "FROM notification_templates WHERE id = :sid"
            ),
            {"nid": str(ULID()), "sid": sid},
        )
    # Watched-item rows moved into watched_item_notification_templates above, so remove them
    # from the library. Domain rows STAY in notification_templates (pre-#200 they were library
    # templates pointed at by domain_nc_refs) — deleting them would cascade-delete the
    # domain_nc_refs rows just re-inserted (FK ondelete=CASCADE).
    conn.execute(
        sa.text("DELETE FROM notification_templates WHERE visibility = 'watched_item'")
    )

    op.drop_index("ix_notification_templates_watched_item_id", "notification_templates")
    op.drop_index("ix_notification_templates_domain_name", "notification_templates")
    op.drop_constraint(
        "notification_templates_watched_item_id_fkey", "notification_templates", type_="foreignkey"
    )
    op.drop_constraint(
        "notification_templates_domain_name_fkey", "notification_templates", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_notification_templates_visibility_refs", "notification_templates", type_="check"
    )
    op.drop_column("notification_templates", "watched_item_id")
    op.drop_column("notification_templates", "domain_name")
    op.drop_column("notification_templates", "visibility")
