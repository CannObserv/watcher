"""One-off: export the operator intent Archiver's migration imports (#373).

Archiver takes over Watcher's operator surface (#368): domains (archiver#360),
item cadence (archiver#364), tags (archiver#362) and notification templates
(archiver#363). This writes the rows each of those imports as one JSON file,
shaped so a consumer reads its slice without reading Watcher code — items keyed
by ``archiver_info_item_id``, domains by name, an item-scoped template naming
the InfoItem rather than Watcher's own id. Item ``name`` and ``description`` are
retired, not ported (design D6): they travel for reference only.

    source scripts/load-env.sh
    WATCHER_ALLOW_PRODUCTION_DB=1 uv run python -m src.ops.export_for_archiver \\
        --output /path/to/watcher-export-YYYY-MM-DD.json

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


def _profile(profile: TemporalProfile | None) -> dict | None:
    if profile is None:
        return None
    return {
        "profile_type": str(profile.profile_type),
        "reference_date": _date(profile.reference_date),
        "date_range_start": _date(profile.date_range_start),
        "date_range_end": _date(profile.date_range_end),
        "rules": profile.rules,
        "post_action": str(profile.post_action),
        "is_active": profile.is_active,
    }


async def collect(session: AsyncSession) -> dict:
    """Read the three slices, every row, archived and inactive included.

    Archiver decides what to import; the file does not filter.
    """
    rows = await session.execute(
        select(WatchedItem, TemporalProfile).outerjoin(
            TemporalProfile, TemporalProfile.watched_item_id == WatchedItem.id
        )
    )
    items: dict[str, dict] = {}
    info_item_by_watched_item: dict[str, str] = {}
    for wi, profile in rows.all():
        info_item_by_watched_item[str(wi.id)] = str(wi.archiver_info_item_id)
        items[str(wi.archiver_info_item_id)] = {
            "name": wi.name,
            "description": wi.description,
            "default_tags": wi.default_tags,
            "default_schedule_config": wi.default_schedule_config,
            "announced_schedule_config": wi.announced_schedule_config,
            "throttle_floor_interval": wi.throttle_floor_interval,
            "archived_at": _timestamp(wi.archived_at),
            "temporal_profile": _profile(profile),
        }

    domains = {
        d.name: {
            "min_interval": d.min_interval,
            "default_schedule_config": d.default_schedule_config,
            "notes": d.notes,
            "is_active": d.is_active,
            "archived_at": _timestamp(d.archived_at),
        }
        for d in (await session.execute(select(Domain))).scalars()
    }

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
        for t in (
            await session.execute(select(NotificationTemplate).order_by(NotificationTemplate.id))
        ).scalars()
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

    if not args.output.parent.is_dir():
        print(f"{args.output.parent} is not a directory", file=sys.stderr)
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
