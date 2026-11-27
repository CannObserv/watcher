"""fetch_headers / fetch_credential_headers on watched_items and fetch_commands (#457)

Revision ID: 3f9a6c1e8b47
Revises: 6b41fb81304a
Create Date: 2026-11-27

The registry's fetch-header policy for an item's source
(``RegistryAnnouncementState.fetch_headers`` / ``fetch_credential_headers``,
cannobserv#780, co-core 0.53.2), mirrored onto the item by the
``info.registry`` reconcile and snapshotted onto each command, whose publish
merges them into ``ContentFetchCommand.headers`` / ``credential_headers``.

Nullable JSONB, with no default and no back-fill: NULL is no policy, which is
the command Watcher sent before this column existed. The models declare
``JSONB(none_as_null=True)`` so ``None`` stays SQL NULL (#198).

No CHECK: what makes a set sendable is ``ContentFetchCommandEmit``'s rule plus
the item's render mode, which a migration cannot freeze. The issue path checks
it before any row exists (``src/core/fetch_commands.py``).

Additive, so the order is migrate, then restart: the running code never names
the columns. No grants step: ``watcher`` owns both tables, and ``watcher_app``'s
table grants cover a new column (#259).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "3f9a6c1e8b47"
down_revision: str | None = "6b41fb81304a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("watched_items", "fetch_commands")
_COLUMNS = ("fetch_headers", "fetch_credential_headers")


def upgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.add_column(table, sa.Column(column, JSONB(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        for column in reversed(_COLUMNS):
            op.drop_column(table, column)
