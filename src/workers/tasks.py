"""Check-watch pipeline and procrastinate task wrappers."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import procrastinate
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.database import get_session_factory
from src.core.differ import ChangeStatus, ChunkFingerprint, diff_chunks
from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.extractors.base import ExtractionResult
from src.core.fetchers.http import HttpFetcher
from src.core.logging import get_logger
from src.core.models.audit_log import AuditLog
from src.core.models.base import generate_ulid
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch
from src.core.rate_limiter import DomainRateLimiter
from src.core.scheduler import compute_next_check
from src.core.simhash import simhash
from src.core.storage import LocalStorage, StorageBackend
from src.workers import bp

logger = get_logger(__name__)

# Shared resources (created once per worker process)
_fetcher = HttpFetcher()
_rate_limiter = DomainRateLimiter()

STORAGE_BASE_DIR = Path(os.environ.get("WATCHER_DATA_DIR", "/var/lib/watcher/data"))

_INT64_MAX = (1 << 63) - 1


def _to_signed64(val: int) -> int:
    """Convert unsigned 64-bit simhash to signed int64 for PostgreSQL BIGINT."""
    if val > _INT64_MAX:
        return val - (1 << 64)
    return val


_EXT_MAP = {
    ContentType.HTML: "html",
    ContentType.PDF: "pdf",
    ContentType.FILE: "csv",
}

_EXTRACTOR_MAP = {
    ContentType.HTML: HtmlExtractor,
    ContentType.PDF: PdfExtractor,
    ContentType.FILE: CsvExcelExtractor,
}


async def _get_previous_snapshot(
    session: AsyncSession, watch_id: object,
) -> Snapshot | None:
    """Fetch most recent snapshot for a watch, or None."""
    stmt = (
        select(Snapshot)
        .where(Snapshot.watch_id == watch_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_snapshot_chunks(
    session: AsyncSession, snapshot_id: object,
) -> list[SnapshotChunk]:
    """Fetch all chunks for a snapshot ordered by index."""
    stmt = (
        select(SnapshotChunk)
        .where(SnapshotChunk.snapshot_id == snapshot_id)
        .order_by(SnapshotChunk.chunk_index)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _extract_content(watch: Watch, raw_content: bytes) -> ExtractionResult:
    """Run the appropriate extractor based on watch content_type."""
    extractor_cls = _EXTRACTOR_MAP[watch.content_type]
    extractor = extractor_cls()
    config: dict | None = None
    if watch.content_type == ContentType.FILE:
        config = {"content_type": "csv"}
    return extractor.extract(raw_content, config=config)


async def _run_check_pipeline(
    watch: Watch,
    raw_content: bytes,
    fetcher_used: str,
    fetch_duration_ms: int,
    storage: StorageBackend,
    session: AsyncSession,
) -> dict:
    """Core check pipeline: hash, extract, diff, store.

    Returns dict with snapshot_id, is_changed, change_id, chunk_count, storage_path.
    """
    # 1. Compute content hash and doc-level simhash
    content_hash = hashlib.sha256(raw_content).hexdigest()
    doc_simhash = _to_signed64(simhash(raw_content.decode(errors="replace")))

    # 2. Check previous snapshot
    prev_snapshot = await _get_previous_snapshot(session, watch.id)

    # 3. Fast path: identical content
    if prev_snapshot and prev_snapshot.content_hash == content_hash:
        logger.info("no change detected", extra={"watch_id": str(watch.id)})
        session.add(AuditLog(
            event_type="check.no_change",
            watch_id=watch.id,
            payload={"content_hash": content_hash},
        ))
        await session.flush()
        return {
            "snapshot_id": None,
            "is_changed": False,
            "change_id": None,
            "chunk_count": 0,
            "storage_path": None,
        }

    # 4. Extract content
    extraction = _extract_content(watch, raw_content)

    # 5. Store raw + extracted text
    snapshot_id = generate_ulid()
    ext = _EXT_MAP[watch.content_type]
    raw_path = storage.snapshot_path(str(watch.id), str(snapshot_id), ext)
    text_path = storage.snapshot_path(str(watch.id), str(snapshot_id), "txt")
    storage.save(raw_path, raw_content)
    full_text = "\n".join(c.text for c in extraction.chunks)
    storage.save(text_path, full_text.encode())

    # 6. Create Snapshot record
    snapshot = Snapshot(
        id=snapshot_id,
        watch_id=watch.id,
        content_hash=content_hash,
        simhash=doc_simhash,
        storage_path=raw_path,
        text_path=text_path,
        storage_backend="local",
        chunk_count=len(extraction.chunks),
        text_bytes=len(full_text.encode()),
        fetch_duration_ms=fetch_duration_ms,
        fetcher_used=fetcher_used,
    )
    session.add(snapshot)
    await session.flush()

    # 7. Create SnapshotChunk records
    for chunk in extraction.chunks:
        session.add(SnapshotChunk(
            snapshot_id=snapshot_id,
            chunk_index=chunk.index,
            chunk_type=chunk.chunk_type,
            chunk_label=chunk.label,
            content_hash=chunk.content_hash,
            simhash=_to_signed64(chunk.simhash),
            char_count=chunk.char_count,
            excerpt=chunk.excerpt,
        ))
    await session.flush()

    # 8-9. Diff against previous if exists
    change_id = None
    if prev_snapshot:
        prev_chunks_db = await _get_snapshot_chunks(session, prev_snapshot.id)
        prev_fingerprints = [
            ChunkFingerprint(
                index=c.chunk_index, label=c.chunk_label,
                content_hash=c.content_hash, simhash=c.simhash,
            )
            for c in prev_chunks_db
        ]
        curr_fingerprints = [
            ChunkFingerprint(
                index=c.index, label=c.label,
                content_hash=c.content_hash, simhash=c.simhash,
            )
            for c in extraction.chunks
        ]
        changes = diff_chunks(prev_fingerprints, curr_fingerprints)
        has_real_changes = any(
            ch.status in (ChangeStatus.ADDED, ChangeStatus.REMOVED, ChangeStatus.MODIFIED)
            for ch in changes
        )
        if has_real_changes:
            metadata = {
                "added": [c.chunk_label for c in changes if c.status == ChangeStatus.ADDED],
                "removed": [c.chunk_label for c in changes if c.status == ChangeStatus.REMOVED],
                "modified": [
                    {"label": c.chunk_label, "similarity": c.similarity}
                    for c in changes if c.status == ChangeStatus.MODIFIED
                ],
            }
            change = Change(
                watch_id=watch.id,
                previous_snapshot_id=prev_snapshot.id,
                current_snapshot_id=snapshot_id,
                change_metadata=metadata,
            )
            session.add(change)
            await session.flush()
            change_id = change.id

    # 10. Audit log
    session.add(AuditLog(
        event_type="check.snapshot_created",
        watch_id=watch.id,
        payload={
            "snapshot_id": str(snapshot_id),
            "content_hash": content_hash,
            "chunk_count": len(extraction.chunks),
            "is_changed": change_id is not None or prev_snapshot is None,
        },
    ))
    await session.flush()

    # 11. Return result
    return {
        "snapshot_id": str(snapshot_id),
        "is_changed": change_id is not None or prev_snapshot is None,
        "change_id": str(change_id) if change_id else None,
        "chunk_count": len(extraction.chunks),
        "storage_path": raw_path,
    }


# --- Procrastinate task wrappers ---


@bp.task(
    name="check_watch",
    queue="default",
    retry=procrastinate.RetryStrategy(
        max_attempts=3,
        exponential_wait=5,
        retry_exceptions={ConnectionError, TimeoutError},
    ),
)
async def check_watch(watch_id: str) -> dict:
    """Fetch and check a single watch for changes."""
    async with get_session_factory()() as session:
        watch = await session.get(Watch, ULID.from_str(watch_id))
        if not watch or not watch.is_active:
            logger.warning("watch not found or inactive", extra={"watch_id": watch_id})
            return {"skipped": True}

        # Fetch with rate limiting — only pass fetcher-relevant config keys
        fetch_config = {
            k: v for k, v in (watch.fetch_config or {}).items()
            if k in ("headers", "timeout")
        }
        async with _rate_limiter.acquire(watch.url):
            fetch_result = await _fetcher.fetch(watch.url, config=fetch_config)

        if fetch_result.status_code == 429:
            _rate_limiter.report_rate_limited(watch.url)
            raise ConnectionError(f"Rate limited by {watch.url}")

        if not fetch_result.is_success:
            logger.warning(
                "fetch failed",
                extra={"watch_id": watch_id, "status": fetch_result.status_code},
            )
            session.add(AuditLog(
                event_type="check.fetch_failed",
                watch_id=watch.id,
                payload={"status_code": fetch_result.status_code},
            ))
            await session.commit()
            return {"error": f"HTTP {fetch_result.status_code}"}

        # Run pipeline
        storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=fetch_result.content,
            fetcher_used=fetch_result.fetcher_used,
            fetch_duration_ms=fetch_result.duration_ms,
            storage=storage,
            session=session,
        )

        # Update last_checked_at
        watch.last_checked_at = datetime.now(UTC)
        await session.commit()

    # schedule_tick is the sole scheduler — no self-deferral here.
    # This avoids double-deferral races and keeps scheduling logic
    # in one place (important for Phase 4 temporal profiles).
    return result


@bp.periodic(cron="* * * * *")
@bp.task(name="schedule_tick", queue="default")
async def schedule_tick(timestamp: int) -> None:
    """Find active watches due for checking and defer check_watch jobs."""
    now = datetime.now(UTC)

    # Load all active watches that might be due. Can't filter by interval in SQL
    # (per-watch JSONB config), so we load all and filter in Python.
    # Acceptable at 2,000 watches; revisit if scale increases significantly.
    async with get_session_factory()() as session:
        stmt = select(Watch).where(
            Watch.is_active.is_(True),
            or_(
                Watch.last_checked_at.is_(None),
                Watch.last_checked_at < now,
            ),
        )
        result = await session.execute(stmt)
        watches = list(result.scalars().all())

    deferred = 0
    for watch in watches:
        next_due = compute_next_check(
            schedule_config=watch.schedule_config or {},
            last_checked_at=watch.last_checked_at,
            now=now,
        )
        if next_due <= now:
            logger.info("deferring check", extra={"watch_id": str(watch.id)})
            await check_watch.configure().defer_async(watch_id=str(watch.id))
            deferred += 1

    if deferred:
        logger.info("schedule_tick deferred checks", extra={"count": deferred})
