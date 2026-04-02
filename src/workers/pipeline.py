"""Check-watch pipeline: content extraction, diffing, and snapshot persistence."""

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.differ import ChangeStatus, ChunkFingerprint, diff_chunks
from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.extractors.base import Chunk, ExtractionResult
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.base import generate_ulid
from src.core.models.change import Change
from src.core.models.domain import Domain
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import Watch
from src.core.rate_limiter import DomainRateLimiter
from src.core.screenshot import capture_screenshot
from src.core.simhash import simhash
from src.core.storage import StorageBackend

logger = get_logger(__name__)

_INT64_MAX = (1 << 63) - 1


def _apply_ignore_patterns(chunks: list[Chunk], ignore_patterns: list[str]) -> list[Chunk]:
    """Filter out chunks whose text fully matches any ignore pattern."""
    if not ignore_patterns:
        return chunks
    compiled = []
    for p in ignore_patterns:
        try:
            compiled.append(re.compile(p))
        except re.error:
            logger.warning("invalid ignore_pattern skipped", extra={"pattern": p})
    return [c for c in chunks if not any(r.fullmatch(c.text) for r in compiled)]


def _compute_significance(*, added: int, removed: int, modified: int, total_curr: int) -> float:
    """Compute change significance as fraction of unchanged chunks.

    Returns 1.0 - (added + removed + modified) / total_curr, clamped to [0.0, 1.0].
    1.0 = no chunks changed; 0.0 = all or more chunks changed (including when
    removed exceeds total_curr, which clamps to 0.0).
    """
    if total_curr == 0:
        return 1.0
    changed = added + removed + modified
    sig = 1.0 - changed / total_curr
    return max(0.0, min(1.0, sig))


_EXT_MAP = {
    "html": "html",
    "pdf": "pdf",
    "file": "csv",
}

_EXTRACTOR_MAP = {
    "html": HtmlExtractor,
    "pdf": PdfExtractor,
    "file": CsvExcelExtractor,
}


def _to_signed64(val: int) -> int:
    """Convert unsigned 64-bit simhash to signed int64 for PostgreSQL BIGINT."""
    if val > _INT64_MAX:
        return val - (1 << 64)
    return val


async def _persist_backoff(domain_name: str, new_interval: float, session: AsyncSession) -> None:
    """Persist backoff state to the Domain table after a 429 response.

    Caller is responsible for committing the session after this call.
    """
    stmt = select(Domain).where(Domain.name == domain_name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()
    if domain is None:
        return
    domain.current_interval = new_interval
    domain.last_request_at = datetime.now(UTC)


async def _maybe_decay_backoff(
    domain_name: str,
    limiter: DomainRateLimiter,
    session: AsyncSession,
) -> bool:
    """Check if a domain's backoff should decay and reset if so.

    Returns True if decay was applied, False otherwise.
    Caller is responsible for committing the session.
    """
    stmt = select(Domain).where(Domain.name == domain_name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()
    if domain is None:
        return False
    if domain.current_interval <= domain.min_interval:
        return False
    if domain.last_request_at is None:
        return False

    elapsed = (datetime.now(UTC) - domain.last_request_at).total_seconds()
    if elapsed < domain.decay_window:
        return False

    domain.current_interval = domain.min_interval
    domain.last_request_at = None
    limiter.reset_domain_interval(domain_name, domain.min_interval)
    logger.info(
        "backoff decayed",
        extra={"domain": domain_name, "reset_to": domain.min_interval},
    )
    return True


async def _get_previous_snapshot(
    session: AsyncSession,
    watch_id: object,
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
    session: AsyncSession,
    snapshot_id: object,
) -> list[SnapshotChunk]:
    """Fetch all chunks for a snapshot ordered by index."""
    stmt = (
        select(SnapshotChunk)
        .where(SnapshotChunk.snapshot_id == snapshot_id)
        .order_by(SnapshotChunk.chunk_index)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _extract_content(watch: Watch, raw_content: bytes) -> ExtractionResult:
    """Run the appropriate extractor based on watch content_type.

    For FILE watches, passes fetch_config extraction settings (e.g., content_type,
    chunk_row_size, sort_columns) through to CsvExcelExtractor.
    """
    ct = str(watch.content_type).lower()
    extractor_cls = _EXTRACTOR_MAP[ct]
    extractor = extractor_cls()
    config: dict | None = None
    if ct == "file":
        fetch_cfg = watch.fetch_config or {}
        config = {
            "content_type": fetch_cfg.get("file_format", "csv"),
            **{
                k: v
                for k, v in fetch_cfg.items()
                if k in ("chunk_row_size", "sort_columns", "sheet_name")
            },
        }
    return await extractor.extract(raw_content, config=config)


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
        audit(session, EventType.CHECK_NO_CHANGE, watch_id=watch.id, content_hash=content_hash)
        await session.flush()
        return {
            "snapshot_id": None,
            "is_changed": False,
            "change_id": None,
            "chunk_count": 0,
            "storage_path": None,
            "change_metadata": {},
        }

    # 4. Extract content
    extraction = await _extract_content(watch, raw_content)

    # 4a. Apply ignore patterns — filter before diffing and persisting
    fetch_cfg = watch.fetch_config or {}
    ignore_patterns: list[str] = fetch_cfg.get("ignore_patterns", [])
    filtered_chunks = _apply_ignore_patterns(extraction.chunks, ignore_patterns)

    # 5. Store raw + extracted text
    snapshot_id = generate_ulid()
    ext = _EXT_MAP[str(watch.content_type).lower()]
    raw_path = storage.snapshot_path(str(watch.id), str(snapshot_id), ext)
    text_path = storage.snapshot_path(str(watch.id), str(snapshot_id), "txt")
    storage.save(raw_path, raw_content)
    full_text = "\n".join(c.text for c in filtered_chunks)
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
        chunk_count=len(filtered_chunks),
        text_bytes=len(full_text.encode()),
        fetch_duration_ms=fetch_duration_ms,
        fetcher_used=fetcher_used,
    )
    session.add(snapshot)
    await session.flush()

    # 7. Create SnapshotChunk records
    for chunk in filtered_chunks:
        session.add(
            SnapshotChunk(
                snapshot_id=snapshot_id,
                chunk_index=chunk.index,
                chunk_type=chunk.chunk_type,
                chunk_label=chunk.label,
                content_hash=chunk.content_hash,
                simhash=_to_signed64(chunk.simhash),
                char_count=chunk.char_count,
                excerpt=chunk.excerpt,
            )
        )
    await session.flush()

    # 8-9. Diff against previous if exists
    change_id = None
    metadata: dict = {}
    if prev_snapshot:
        prev_chunks_db = await _get_snapshot_chunks(session, prev_snapshot.id)
        prev_fingerprints = [
            ChunkFingerprint(
                index=c.chunk_index,
                label=c.chunk_label,
                content_hash=c.content_hash,
                simhash=c.simhash,
            )
            for c in prev_chunks_db
        ]
        curr_fingerprints = [
            ChunkFingerprint(
                index=c.index,
                label=c.label,
                content_hash=c.content_hash,
                simhash=c.simhash,
            )
            for c in filtered_chunks
        ]
        changes = diff_chunks(prev_fingerprints, curr_fingerprints)
        has_real_changes = any(
            ch.status in (ChangeStatus.ADDED, ChangeStatus.REMOVED, ChangeStatus.MODIFIED)
            for ch in changes
        )
        if has_real_changes:
            n_added = sum(1 for c in changes if c.status == ChangeStatus.ADDED)
            n_removed = sum(1 for c in changes if c.status == ChangeStatus.REMOVED)
            n_modified = sum(1 for c in changes if c.status == ChangeStatus.MODIFIED)
            significance = _compute_significance(
                added=n_added,
                removed=n_removed,
                modified=n_modified,
                total_curr=len(filtered_chunks),
            )
            metadata = {
                "added": [c.chunk_label for c in changes if c.status == ChangeStatus.ADDED],
                "removed": [c.chunk_label for c in changes if c.status == ChangeStatus.REMOVED],
                "modified": [
                    {"label": c.chunk_label, "similarity": c.similarity}
                    for c in changes
                    if c.status == ChangeStatus.MODIFIED
                ],
            }
            change = Change(
                watch_id=watch.id,
                previous_snapshot_id=prev_snapshot.id,
                current_snapshot_id=snapshot_id,
                change_metadata=metadata,
                significance=significance,
            )
            session.add(change)
            await session.flush()
            change_id = change.id

    # 10. Audit log
    audit(
        session,
        EventType.CHECK_SNAPSHOT_CREATED,
        watch_id=watch.id,
        snapshot_id=str(snapshot_id),
        content_hash=content_hash,
        chunk_count=len(filtered_chunks),
        is_changed=change_id is not None or prev_snapshot is None,
    )
    await session.flush()

    # 11. Screenshot (optional — HTML only; non-fatal if Playwright not installed or capture fails)
    screenshot_path: str | None = None
    if str(watch.content_type).lower() == "html":
        try:
            screenshot_result = await capture_screenshot(watch.url)
            if screenshot_result is not None:
                screenshot_path = storage.snapshot_path(str(watch.id), str(snapshot_id), "png")
                storage.save(screenshot_path, screenshot_result.png_bytes)
                snapshot.screenshot_path = screenshot_path
                snapshot.screenshot_browser = screenshot_result.browser
                await session.flush()
        except Exception as exc:
            logger.warning("screenshot step failed for watch %s: %s", str(watch.id), exc)

    # 12. Return result
    return {
        "snapshot_id": str(snapshot_id),
        "is_changed": change_id is not None or prev_snapshot is None,
        "change_id": str(change_id) if change_id else None,
        "chunk_count": len(filtered_chunks),
        "storage_path": raw_path,
        "screenshot_path": screenshot_path,
        "change_metadata": metadata if change_id else {},
    }
