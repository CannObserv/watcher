"""#168: drop the stored media_type_essence generated column

The dispatch essence is a pure function of content_media_type + effective_url
(`media_type.resolve_dispatch_essence`) — the same value the pipeline dispatches
on — and is surfaced as a computed field on WatchedItemResponse. Nothing queries
the column in SQL, so the stored/generated projection is removed in favour of one
Python source of truth (no Python/SQL parity, no MissingGreenlet refresh).

Revision ID: a3b1c2d4e5f6
Revises: f1c0ffee2168
Create Date: 2026-06-23 13:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3b1c2d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f1c0ffee2168'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ESSENCE_SQL = "lower(btrim(split_part(content_media_type, ';', 1)))"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("watched_items", "media_type_essence")


def downgrade() -> None:
    """Downgrade schema — restore the STORED generated projection."""
    op.add_column(
        "watched_items",
        sa.Column(
            "media_type_essence",
            sa.Text(),
            sa.Computed(_ESSENCE_SQL, persisted=True),
            nullable=True,
        ),
    )
