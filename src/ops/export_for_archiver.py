"""One-off: export the operator intent Archiver's migration imports (#373).

Archiver takes over Watcher's operator surface (#368): domains (archiver#360),
item cadence (archiver#364), tags (archiver#362) and notification templates
(archiver#363). This writes the rows each of those imports as one JSON file,
shaped so a consumer reads its slice without reading Watcher code — items keyed
by ``archiver_info_item_id``, domains by name, an item-scoped template naming
the InfoItem rather than Watcher's own id. Item ``name`` and ``description`` are
retired, not ported (design D6): they travel for reference only.

    source scripts/load-env.sh
    d=$(mktemp -d)   # 0700, yours: the command cannot write a root-only path itself
    WATCHER_ALLOW_PRODUCTION_DB=1 uv run python -m src.ops.export_for_archiver \\
        --output "$d/watcher-export-YYYY-MM-DD.json"
    sudo install -D -m 600 -o root -g root "$d/watcher-export-YYYY-MM-DD.json" \\
        /root/archiver-export/watcher-export-YYYY-MM-DD.json
    shred -u "$d"/watcher-export-*.json; rmdir "$d"   # leave no second copy

Re-run it before each flip that consumes it (#374, #375, #376): the operator can
change data in between. Keys and rows are sorted, so two runs differ only in
``exported_at`` unless the data did.

Read-only by construction: one ``REPEATABLE READ, READ ONLY`` transaction, so
the three tables come from one snapshot and Postgres refuses any write. The
file holds notifier channel ids and channel hints, and both repos are public:
it is written owner-only, its contents never reach stdout (counts,
``exported_at``, the sha256 and the path do), and it is never attached to an
issue. Deleted in the jump (#380).
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.core.db_safety import (
    ALLOW_PRODUCTION_DB_ENV,
    ProductionDatabaseRefused,
    assert_environment_db_allowed,
)
from src.core.logging import configure_logging
from src.core.models.domain import Domain
from src.core.models.notification_template import NotificationTemplate
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watched_item import WatchedItem

#: Bumped on any change a consumer would have to read differently.
SCHEMA_VERSION = 1


def _timestamp(value: datetime | None) -> str | None:
    """Fixed-width ``…ffffffZ`` (AGENTS.md), so a re-run diffs cleanly."""
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _profile(row) -> dict | None:
    if row.profile_type is None:  # the outer join found no profile
        return None
    return {
        "profile_type": str(row.profile_type),
        "reference_date": _date(row.reference_date),
        "date_range_start": _date(row.date_range_start),
        "date_range_end": _date(row.date_range_end),
        "rules": row.rules,
        "post_action": str(row.post_action),
        "is_active": row.profile_is_active,
    }


async def collect(session: AsyncSession) -> dict:
    """Read the three slices, every row, archived and inactive included.

    Archiver decides what to import; the file does not filter. Only the
    exported columns are selected, never whole entities: a checkout ahead of
    the database's migrations, or a flip that drops a column the file never
    carried, must not break a re-run (CR 11).
    """
    item_rows = await session.execute(
        select(
            WatchedItem.id,
            WatchedItem.archiver_info_item_id,
            WatchedItem.name,
            WatchedItem.description,
            WatchedItem.default_tags,
            WatchedItem.default_schedule_config,
            WatchedItem.announced_schedule_config,
            WatchedItem.throttle_floor_interval,
            WatchedItem.archived_at,
            TemporalProfile.profile_type,
            TemporalProfile.reference_date,
            TemporalProfile.date_range_start,
            TemporalProfile.date_range_end,
            TemporalProfile.rules,
            TemporalProfile.post_action,
            TemporalProfile.is_active.label("profile_is_active"),
        ).outerjoin(TemporalProfile, TemporalProfile.watched_item_id == WatchedItem.id)
    )
    items: dict[str, dict] = {}
    info_item_by_watched_item: dict[str, str] = {}
    for row in item_rows:
        info_item_by_watched_item[str(row.id)] = str(row.archiver_info_item_id)
        items[str(row.archiver_info_item_id)] = {
            "name": row.name,
            "description": row.description,
            "default_tags": row.default_tags,
            "default_schedule_config": row.default_schedule_config,
            "announced_schedule_config": row.announced_schedule_config,
            "throttle_floor_interval": row.throttle_floor_interval,
            "archived_at": _timestamp(row.archived_at),
            "temporal_profile": _profile(row),
        }

    domain_rows = await session.execute(
        select(
            Domain.name,
            Domain.min_interval,
            Domain.default_schedule_config,
            Domain.notes,
            Domain.is_active,
            Domain.archived_at,
        )
    )
    domains = {
        d.name: {
            "min_interval": d.min_interval,
            "default_schedule_config": d.default_schedule_config,
            "notes": d.notes,
            "is_active": d.is_active,
            "archived_at": _timestamp(d.archived_at),
        }
        for d in domain_rows
    }

    template_rows = await session.execute(
        select(
            NotificationTemplate.id,
            NotificationTemplate.title,
            NotificationTemplate.channel_hint,
            NotificationTemplate.events,
            NotificationTemplate.visibility,
            NotificationTemplate.domain_name,
            NotificationTemplate.watched_item_id,
            NotificationTemplate.is_active,
            NotificationTemplate.content_config,
            NotificationTemplate.remote_channel_id,
        ).order_by(NotificationTemplate.id)
    )
    templates = [
        {
            "id": str(t.id),
            "title": t.title,
            "channel_hint": t.channel_hint,
            "events": t.events,
            "visibility": t.visibility,
            "domain_name": t.domain_name,
            "archiver_info_item_id": (
                info_item_by_watched_item[str(t.watched_item_id)]
                if t.watched_item_id is not None
                else None
            ),
            "is_active": t.is_active,
            "content_config": t.content_config,
            "remote_channel_id": t.remote_channel_id,
        }
        for t in template_rows
    ]

    return {"items": items, "domains": domains, "notification_templates": templates}


def build_document(data: Mapping, *, exported_at: datetime) -> dict:
    """Wrap the collected slices with the provenance a consumer checks first."""
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "watcher",
        "exported_at": _timestamp(exported_at),
        **data,
    }


def render(document: Mapping) -> str:
    """Deterministic JSON: sorted keys, two-space indent, trailing newline."""
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_hex(body: str) -> str:
    """The digest recorded on the issue at handover, in place of the contents."""
    return hashlib.sha256(body.encode()).hexdigest()


def write_private(path: Path, body: str) -> None:
    """Write ``body`` owner-only (``0600``), atomically.

    A fresh ``mkstemp`` file (``0600``, ``O_EXCL``) beside ``path`` is renamed
    over it, so a re-run that fails part way leaves the previous export whole —
    it is the one that may be carried — and a symlink at ``path`` is replaced
    rather than written through. A loosened file is replaced by a tight one.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


@asynccontextmanager
async def read_only_session(url: str) -> AsyncIterator[AsyncSession]:
    """A session in one ``REPEATABLE READ, READ ONLY`` transaction on its own engine."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            conn = await conn.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            async with conn.begin():
                async with AsyncSession(bind=conn) as session:
                    yield session
    finally:
        await engine.dispose()


async def _export(url: str) -> dict:
    async with read_only_session(url) as session:
        # Transaction start, on the database's clock: the moment the data is as of.
        snapshot_at = (await session.execute(select(func.now()))).scalar_one()
        data = await collect(session)
    return build_document(data, exported_at=snapshot_at)


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] = os.environ) -> int:
    """CLI entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="file to write, owner-only (there is no stdout mode)",
    )
    args = parser.parse_args(argv)

    configure_logging()
    url = environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set — source scripts/load-env.sh", file=sys.stderr)
        return 2
    try:
        assert_environment_db_allowed(environ)
    except ProductionDatabaseRefused as e:
        print(
            f"{e}\n  To export production deliberately, set {ALLOW_PRODUCTION_DB_ENV}=1 "
            "for this one command.",
            file=sys.stderr,
        )
        return 2

    # Before the read, not after it. os.access, not Path.is_dir: is_dir raises
    # on a directory the operator cannot traverse (``/root/…``), and passes one
    # they cannot write.
    parent = args.output.parent
    if not (os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK)):
        print(
            f"{parent} is not a directory you can write. Write to one you own, move "
            "it into place with sudo install -m 600 -o root -g root, then shred your "
            "copy (see this module's docstring)",
            file=sys.stderr,
        )
        return 2

    document = asyncio.run(_export(url))
    body = render(document)
    write_private(args.output, body)

    for key in ("items", "domains", "notification_templates"):
        print(f"{key}: {len(document[key])}")
    print(f"exported_at: {document['exported_at']}")
    print(f"sha256: {sha256_hex(body)}")
    print(f"wrote {args.output} (0600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
