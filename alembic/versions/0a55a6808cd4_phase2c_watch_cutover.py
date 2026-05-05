"""phase2c watch cutover — backfill info_item rows, drop watches.url + fetch_config

Revision ID: 0a55a6808cd4
Revises: 9e86f9e4d704
Create Date: 2026-05-05 03:03:13.898605

This migration finalises the Phase 2c cutover. Pre-state: ``watches`` carries
both legacy fetch metadata (``url`` + ``fetch_config``) and a nullable
``info_item_id`` cross-schema FK. Post-state: every watch is linked to an
Information service ``info_items`` row whose primary ``info_specs`` row holds
the fetch target + extraction selector; the legacy columns are gone.

Pre-flight guards abort the upgrade if any watch is non-HTML or uses a
``fetch_config`` key that the v1 InfoSpec schema cannot represent. ``timeout``
is intentionally accepted — it maps to ``target.fetch.timeout_seconds``.

Downgrade re-adds the columns nullable, copies ``target.url`` and
``extraction.selector`` back, and leaves the InfoItem/InfoSpec rows in place
(operator may choose to delete them manually).
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from ulid import ULID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0a55a6808cd4"
down_revision: str | Sequence[str] | None = "9e86f9e4d704"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# fetch_config keys that the v1 InfoSpec schema cannot represent.
# ``timeout`` is intentionally NOT in this set — it maps to
# target.fetch.timeout_seconds in the InfoSpec document.
UNSUPPORTED_FETCH_CONFIG_KEYS = frozenset(
    {
        "headers",
        "ignore_patterns",
        "exclude_selectors",
        "ignore_selectors",
        "dynamic_id_patterns",
        "strip_boilerplate",
        "skip_empty_pages",
        "file_format",
        "chunk_row_size",
        "sort_columns",
        "sheet_name",
        "viewport_width",
        "viewport_height",
    }
)


def upgrade() -> None:
    """Backfill InfoItem + InfoSpec rows from existing watches, then drop legacy columns."""
    conn = op.get_bind()

    # Guard 1: non-HTML watches — InfoSpec v1 only supports HTML extraction.
    non_html = conn.execute(
        sa.text("SELECT id, name, content_type FROM watches WHERE content_type != 'html'")
    ).fetchall()
    if non_html:
        raise RuntimeError(
            f"Phase 2c migration aborted: {len(non_html)} non-HTML watches present. "
            "InfoSpec v1 only supports HTML extraction. Manually re-key or delete: "
            f"{[(r.id, r.name) for r in non_html]}"
        )

    # Guard 2: unsupported fetch_config keys.
    rows = conn.execute(sa.text("SELECT id, name, fetch_config FROM watches")).fetchall()
    bad: list[tuple[str, str, list[str]]] = []
    for r in rows:
        keys = set((r.fetch_config or {}).keys())
        extras = keys & UNSUPPORTED_FETCH_CONFIG_KEYS
        if extras:
            bad.append((r.id, r.name, sorted(extras)))
    if bad:
        raise RuntimeError(
            "Phase 2c migration aborted: watches use fetch_config keys that the "
            "v1 InfoSpec schema cannot represent. Either delete the keys, extend "
            f"the InfoSpec schema first, or remove the watches: {bad}"
        )

    # Backfill: one InfoItem + one primary InfoSpec per watch.
    watches = conn.execute(
        sa.text(
            "SELECT id, name, url, fetch_config, content_type FROM watches "
            "WHERE info_item_id IS NULL"
        )
    ).fetchall()

    for w in watches:
        info_item_id = ULID()
        info_spec_id = ULID()
        fc = w.fetch_config or {}
        selectors = fc.get("selectors") or []
        if selectors:
            algorithm = "css"
            selector = ", ".join(selectors)
        else:
            algorithm = "full_page"
            selector = None

        target: dict[str, object] = {"url": w.url}
        if "timeout" in fc:
            target["fetch"] = {"timeout_seconds": int(fc["timeout"])}

        document = {
            "schema_version": 1,
            "target": target,
            "extraction": (
                {"algorithm": algorithm}
                if algorithm == "full_page"
                else {"algorithm": algorithm, "selector": selector}
            ),
            "fingerprint": {"algorithm": "simhash"},
        }

        conn.execute(
            sa.text(
                "INSERT INTO information.info_items "
                "(info_item_id, name, description, owner, created_at, updated_at) "
                "VALUES (:id, :name, NULL, NULL, now(), now())"
            ),
            {"id": str(info_item_id), "name": w.name},
        )
        conn.execute(
            sa.text(
                "INSERT INTO information.info_specs "
                "(info_spec_id, info_item_id, schema_version, document, "
                "priority, active, created_at) "
                "VALUES (:sid, :iid, 1, CAST(:doc AS jsonb), 1, TRUE, now())"
            ),
            {
                "sid": str(info_spec_id),
                "iid": str(info_item_id),
                "doc": json.dumps(document),
            },
        )
        conn.execute(
            sa.text("UPDATE watches SET info_item_id = :iid WHERE id = :wid"),
            {"iid": str(info_item_id), "wid": str(w.id)},
        )

    # Tighten constraints, drop legacy columns.
    op.alter_column("watches", "info_item_id", nullable=False)
    op.drop_column("watches", "url")
    op.drop_column("watches", "fetch_config")


def downgrade() -> None:
    """Re-add legacy columns, copy fetch metadata back from primary InfoSpec.

    Does NOT delete the InfoItem/InfoSpec rows created during upgrade — that's
    an operator decision. ``url`` is left nullable because we cannot guarantee
    every watch has a primary spec; an operator may run ``ALTER COLUMN url SET
    NOT NULL`` after verifying coverage.
    """
    op.alter_column("watches", "info_item_id", nullable=True)
    op.add_column("watches", sa.Column("url", sa.Text(), nullable=True))
    op.add_column(
        "watches",
        sa.Column(
            "fetch_config",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT w.id AS watch_id, s.document AS doc
            FROM watches w
            JOIN information.info_specs s
              ON s.info_item_id = w.info_item_id AND s.active AND s.priority = 1
            """
        )
    ).fetchall()
    for r in rows:
        doc = r.doc if isinstance(r.doc, dict) else json.loads(r.doc)
        url = doc.get("target", {}).get("url")
        extraction = doc.get("extraction", {})
        selectors: list[str] = []
        if extraction.get("algorithm") == "css" and extraction.get("selector"):
            selectors = [s.strip() for s in extraction["selector"].split(",") if s.strip()]
        timeout = doc.get("target", {}).get("fetch", {}).get("timeout_seconds")
        fc: dict[str, object] = {}
        if selectors:
            fc["selectors"] = selectors
        if timeout is not None:
            fc["timeout"] = timeout
        bind.execute(
            sa.text(
                "UPDATE watches SET url = :url, fetch_config = CAST(:fc AS jsonb) WHERE id = :id"
            ),
            {"url": url, "fc": json.dumps(fc), "id": r.watch_id},
        )
