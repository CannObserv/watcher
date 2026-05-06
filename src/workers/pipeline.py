"""Check-watch pipeline: content extraction, diffing, and snapshot persistence."""

import hashlib
from datetime import UTC, datetime

from information_client import InformationClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.differ import ChangeStatus, ChunkFingerprint, diff_chunks
from src.core.extraction_defaults import (
    extraction_config_from_spec as _extraction_config_from_spec,
)
from src.core.extractors import HtmlExtractor
from src.core.extractors.base import ExtractionResult
from src.core.info_resolver import ResolvedInfoSpec, resolve_primary
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.base import generate_ulid
from src.core.models.change import Change
from src.core.models.domain import Domain
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch
from src.core.rate_limiter import DomainRateLimiter
from src.core.screenshot import capture_screenshot
from src.core.simhash import simhash
from src.core.storage import StorageBackend

logger = get_logger(__name__)

_INT64_MAX = (1 << 63) - 1


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


# Phase 2c: only HTML survives the InfoSpec cutover. Migration aborts on
# non-HTML content_type; PDF + FILE pipelines return in Phase 3+.
_EXT_MAP = {
    "html": "html",
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


async def _extract_with_spec(raw_content: bytes, document: dict) -> ExtractionResult:
    """Run the HTML extractor with config derived from the InfoSpec document.

    Phase 2c only supports HTML. PDF + FILE return in Phase 3+ once the
    InfoSpec schema gains the corresponding extraction algorithms.
    """
    extractor = HtmlExtractor()
    config = _extraction_config_from_spec(document)
    return await extractor.extract(raw_content, config=config)


async def _run_check_pipeline(
    watch: Watch,
    raw_content: bytes,
    fetcher_used: str,
    fetch_duration_ms: int,
    storage: StorageBackend,
    session: AsyncSession,
    *,
    resolved: ResolvedInfoSpec,
    info_client: InformationClient | None = None,
) -> dict:
    """Core check pipeline: hash, extract, diff, store.

    Returns dict with snapshot_id, is_changed, change_id, chunk_count, storage_path.

    ``resolved`` carries the primary InfoSpec the caller already fetched (always
    supplied in production by ``check_watch``). ``info_client`` is optional; when
    provided, it's used to force a spec re-fetch on zero-chunk extraction. When
    ``info_client`` is omitted and extraction yields zero chunks, the empty
    result is accepted as-is and proceeds to diff.
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

    # 4. Extract content using the resolved InfoSpec.
    document = resolved.document
    extraction = await _extract_with_spec(raw_content, document)

    # 4a. Force-refresh + retry path: when extraction returns zero chunks,
    # the spec selector may be stale. Refresh the spec and re-run extraction
    # against the same content (no re-fetch).
    if not extraction.chunks and info_client is not None:
        logger.info(
            "extraction returned zero chunks — force-refreshing primary InfoSpec",
            extra={
                "watch_id": str(watch.id),
                "info_item_id": resolved.info_item_id,
                "info_spec_id": resolved.info_spec_id,
            },
        )
        resolved = await resolve_primary(info_client, resolved.info_item_id, force_refresh=True)
        document = resolved.document
        extraction = await _extract_with_spec(raw_content, document)
        if not extraction.chunks:
            logger.warning(
                "extraction still returned zero chunks after force_refresh",
                extra={
                    "watch_id": str(watch.id),
                    "info_item_id": resolved.info_item_id,
                    "info_spec_id": resolved.info_spec_id,
                },
            )

    chunks = extraction.chunks

    # 5. Store raw + extracted text
    snapshot_id = generate_ulid()
    ext = _EXT_MAP.get(str(watch.content_type).lower(), "html")
    raw_path = storage.snapshot_path(str(watch.id), str(snapshot_id), ext)
    text_path = storage.snapshot_path(str(watch.id), str(snapshot_id), "txt")
    storage.save(raw_path, raw_content)
    full_text = "\n".join(c.text for c in chunks)
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
        chunk_count=len(chunks),
        text_bytes=len(full_text.encode()),
        fetch_duration_ms=fetch_duration_ms,
        fetcher_used=fetcher_used,
    )
    session.add(snapshot)
    await session.flush()

    # 7. Create SnapshotChunk records
    for chunk in chunks:
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
            for c in chunks
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
                total_curr=len(chunks),
            )
            metadata = {
                "added": [c.chunk_label for c in changes if c.status == ChangeStatus.ADDED],
                "removed": [c.chunk_label for c in changes if c.status == ChangeStatus.REMOVED],
                "modified": [
                    {"label": c.chunk_label, "similarity": c.similarity}
                    for c in changes
                    if c.status == ChangeStatus.MODIFIED
                ],
                "significance": significance,
            }
            change_kwargs: dict = {
                "watch_id": watch.id,
                "previous_snapshot_id": prev_snapshot.id,
                "current_snapshot_id": snapshot_id,
                "change_metadata": metadata,
                "significance": significance,
                "info_item_id": watch.info_item_id,
                "previous_fingerprint": prev_snapshot.simhash,
                "current_fingerprint": doc_simhash,
            }
            if resolved is not None:
                change_kwargs["info_spec_id"] = ULID.from_str(resolved.info_spec_id)
            change = Change(**change_kwargs)
            session.add(change)
            await session.flush()
            change_id = change.id
            # Stored in metadata so it flows into WatchEvent for the
            # include_change_dashboard_url content option's URL construction.
            metadata["change_id"] = str(change.id)

    # 10. Audit log
    audit(
        session,
        EventType.CHECK_SNAPSHOT_CREATED,
        watch_id=watch.id,
        snapshot_id=str(snapshot_id),
        content_hash=content_hash,
        chunk_count=len(chunks),
        is_changed=change_id is not None or prev_snapshot is None,
    )
    await session.flush()

    # 11. Screenshot (optional — HTML only; non-fatal if Playwright not installed or capture fails)
    screenshot_path: str | None = None
    if watch.content_type == ContentType.HTML:
        screenshot_url = (
            resolved.document.get("target", {}).get("url") if resolved is not None else None
        )
        if screenshot_url:
            try:
                screenshot_result = await capture_screenshot(screenshot_url)
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
        "chunk_count": len(chunks),
        "storage_path": raw_path,
        "screenshot_path": screenshot_path,
        "change_metadata": metadata if change_id else {},
    }
