"""#168: rename default_content_type -> content_media_type (raw MIME) + essence

Slice 1 of #168. The operator-declared ``default_content_type`` enum
(``html``/``pdf``/``file``) becomes the observed, free-form ``content_media_type``
(raw ``Content-Type`` header, e.g. ``text/html; charset=utf-8``), auto-detected
from the first successful fetch. A STORED generated column ``media_type_essence``
projects the lowercased ``type/subtype`` essence (the future extractor dispatch
key) — drift-proof, maintained by Postgres.

Data migration maps the legacy enum values to their MIME equivalents
(``html`` -> ``text/html``, ``pdf`` -> ``application/pdf``); ``file`` and any
other value are nulled (the next fetch re-detects).

Revision ID: f1c0ffee2168
Revises: d5e6f7a8b9c0
Create Date: 2026-06-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1c0ffee2168'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ESSENCE_SQL = "lower(btrim(split_part(content_media_type, ';', 1)))"


def upgrade() -> None:
    """Upgrade schema."""
    # Rename + widen: varchar(20) enum-shaped -> varchar(2048) raw MIME (a sanity
    # cap; real Content-Type headers are tiny).
    op.alter_column(
        "watched_items",
        "default_content_type",
        new_column_name="content_media_type",
        existing_type=sa.String(length=20),
        type_=sa.String(length=2048),
        existing_nullable=True,
    )
    # Map legacy enum values to raw MIME; null the rest (re-detected on next fetch).
    op.execute(
        """
        UPDATE watched_items
           SET content_media_type = CASE content_media_type
               WHEN 'html' THEN 'text/html'
               WHEN 'pdf'  THEN 'application/pdf'
               ELSE NULL
           END
         WHERE content_media_type IS NOT NULL
        """
    )
    # Drift-proof type/subtype essence, maintained by the database.
    op.add_column(
        "watched_items",
        sa.Column(
            "media_type_essence",
            sa.Text(),
            sa.Computed(_ESSENCE_SQL, persisted=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("watched_items", "media_type_essence")
    # Reverse the value map BEFORE narrowing the column so nothing exceeds 20 chars.
    op.execute(
        """
        UPDATE watched_items
           SET content_media_type = CASE
               WHEN content_media_type LIKE 'text/html%'       THEN 'html'
               WHEN content_media_type LIKE 'application/pdf%'  THEN 'pdf'
               ELSE NULL
           END
         WHERE content_media_type IS NOT NULL
        """
    )
    op.alter_column(
        "watched_items",
        "content_media_type",
        new_column_name="default_content_type",
        existing_type=sa.String(length=2048),
        type_=sa.String(length=20),
        existing_nullable=True,
    )
