"""watched_items.render and fetch_commands.render (#414)

Revision ID: 6b41fb81304a
Revises: c8e1d4f7a2b6
Create Date: 2026-11-17

The registry's fetch mode for an item's source (``RegistryAnnouncementState.render``,
cannobserv#602), mirrored onto the item by the ``info.registry`` reconcile and
snapshotted onto each command, which sends it as ``ContentFetchCommand.render``.

Nullable, with no default and no back-fill: NULL means the registry has no
opinion yet, which co-core reads as ``http``, and a NULL row's command is
byte-identical to the frame sent before this column existed.

A CHECK on each table holds the column to the modes ``ContentFetchCommandEmit``
accepts. A value outside them makes the Emit raise at every publish, so the
database refuses to hold one. The modes are spelled out here because a
migration is frozen; ``tests/test_migration_add_render_414.py`` pins them to
co-core's ``RenderMode``, which the models read.

Additive, so the order is migrate, then restart: the running code never names
the column. No grants step: ``watcher`` owns both tables, and ``watcher_app``'s
table grants cover a new column (#259).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b41fb81304a"
down_revision: str | None = "c8e1d4f7a2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENDER_MODES = ("http", "browser")
_TABLES = ("watched_items", "fetch_commands")


def upgrade() -> None:
    modes = ", ".join(f"'{mode}'" for mode in RENDER_MODES)
    for table in _TABLES:
        op.add_column(table, sa.Column("render", sa.String(16), nullable=True))
        op.create_check_constraint(f"ck_{table}_render", table, f"render IN ({modes})")


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_render", table, type_="check")
        op.drop_column(table, "render")
