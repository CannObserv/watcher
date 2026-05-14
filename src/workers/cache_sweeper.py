"""Periodic task: delete stale scratch files + PATCH-cache-clear on Archiver."""

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from ulid import ULID

from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.pending_source_revision import PendingSourceRevision
from src.core.registry import get_registry
from src.workers import bp

logger = get_logger(__name__)

_ULID_FILENAME = re.compile(r"^([0-9A-HJKMNP-TV-Z]{26})\.bin$")


def _cache_dir() -> Path:
    """Return the scratch-cache directory from env, defaulting to /var/cache/watcher/scratch."""
    return Path(os.environ.get("WATCHER_CACHE_DIR", "/var/cache/watcher/scratch"))


def _ttl_seconds() -> int:
    """Return the scratch-file TTL in seconds from env, defaulting to 600."""
    return int(os.environ.get("WATCHER_CACHE_TTL_SECONDS", "600"))


@bp.periodic(cron="* * * * *", periodic_id="sweep_scratch_cache")
@bp.task(name="sweep_scratch_cache", queue="default")
async def sweep_scratch_cache(**periodic_kwargs) -> dict:
    """Delete scratch files older than TTL, skipping files with pending outbox rows.

    Candidates are files whose names match ``<ULID>.bin``. Files whose ULID
    appears in ``pending_source_revisions`` are skipped — those rows own the
    scratch file and the drain worker will remove it upon success.

    After deletion, sends a best-effort PATCH to Archiver to clear the cache
    URI so Archiver knows the file is gone.

    Returns:
        dict with keys ``deleted``, ``skipped``, ``patch_failures``.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=_ttl_seconds())
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return {"deleted": 0, "skipped": 0, "patch_failures": 0}

    candidates: list[tuple[str, Path]] = []
    for p in cache_dir.iterdir():
        if not p.is_file():
            continue
        m = _ULID_FILENAME.match(p.name)
        if not m:
            continue
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
        if mtime > cutoff:
            continue
        candidates.append((m.group(1), p))

    if not candidates:
        return {"deleted": 0, "skipped": 0, "patch_failures": 0}

    candidate_ulids = [ULID.from_str(rid) for rid, _ in candidates]
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PendingSourceRevision.id).where(PendingSourceRevision.id.in_(candidate_ulids))
        )
        reserved = {str(rid) for (rid,) in result.all()}

    deleted = 0
    skipped = 0
    patch_failures = 0
    client = get_registry().get_archiver_client()
    for revision_id, path in candidates:
        if revision_id in reserved:
            skipped += 1
            continue
        try:
            path.unlink()
        except OSError as e:
            logger.warning(
                "scratch delete failed",
                extra={"path": str(path), "error": str(e)},
            )
            continue
        deleted += 1
        try:
            await client.patch_source_revision_cache(
                revision_id,
                content_cache_uri=None,
                content_cache_expires_at=None,
            )
        except Exception as e:
            patch_failures += 1
            logger.warning(
                "patch cache-clear failed",
                extra={"revision_id": revision_id, "error": str(e)},
            )

    logger.info(
        "sweep_scratch_cache finished",
        extra={"deleted": deleted, "skipped": skipped, "patch_failures": patch_failures},
    )
    return {"deleted": deleted, "skipped": skipped, "patch_failures": patch_failures}
