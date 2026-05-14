"""Check-watch pipeline: content extraction, fingerprinting, and SourceRevision POST."""

import hashlib
import os
from datetime import UTC, datetime, timedelta

from archiver_client import ArchiverClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.extraction_defaults import (
    extraction_config_from_spec as _extraction_config_from_spec,
)
from src.core.extractors import HtmlExtractor
from src.core.extractors.base import ExtractionResult
from src.core.logging import get_logger
from src.core.models.domain import Domain
from src.core.models.watch import Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.rate_limiter import DomainRateLimiter
from src.core.sources.outbox import enqueue_pending
from src.core.sources.resolver import ResolvedRootSource
from src.core.sources.revision_cache import get_last_fingerprint, upsert_last_known
from src.core.sources.scratch import (
    allocate_revision_id,
    rename_scratch_to_canonical,
    scratch_path_for,
    write_scratch_bytes,
)
from src.core.storage import StorageBackend

logger = get_logger(__name__)

_INT64_MAX = (1 << 63) - 1

WATCHER_CACHE_TTL_SECONDS = int(os.environ.get("WATCHER_CACHE_TTL_SECONDS", "600"))

# Phase 2c: only HTML survives the InfoSpec cutover. Migration aborts on
# non-HTML content_type; PDF + FILE pipelines return in Phase 3+.
_EXT_MAP = {
    "html": "html",
}


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


async def _extract_with_spec(raw_content: bytes, document: dict) -> ExtractionResult:
    """Run the HTML extractor with config derived from the InfoSpec document.

    Phase 2c only supports HTML. PDF + FILE return in Phase 3+ once the
    InfoSpec schema gains the corresponding extraction algorithms.
    """
    extractor = HtmlExtractor()
    config = _extraction_config_from_spec(document)
    return await extractor.extract(raw_content, config=config)


async def _run_check_pipeline(
    watch,
    raw_content: bytes,
    fetcher_used: str,
    fetch_duration_ms: int,
    storage: StorageBackend | None,
    session: AsyncSession,
    *,
    resolved: ResolvedRootSource,
    info_client: ArchiverClient | None = None,
) -> dict:
    """Fetch → scratch → POST root SourceRevision → dispatch. Outbox on POST failure.

    Returns dict with is_changed, and on success: source_revision_id, scratch_path.
    Returns is_changed=False + skipped_reason="fast_path" when fingerprint is unchanged.
    Returns outbox=True when POST fails (cascade aborted).
    """
    # 1. Extract root content per source_spec.
    root_extracted = await _extract_with_spec(raw_content, resolved.source_spec)
    # Compose bytes from chunk text (joined with newlines, UTF-8 encoded).
    root_bytes = "\n".join(c.text for c in root_extracted.chunks).encode()

    # 2. SHA-256 over post-trim content.
    fingerprint = "sha256:" + hashlib.sha256(root_bytes).hexdigest()

    # 3. Fast-path: local cache.
    prior_fp = await get_last_fingerprint(session, resolved.info_source_id)
    if prior_fp == fingerprint:
        return {"is_changed": False, "skipped_reason": "fast_path"}

    # 4. Allocate ULID, write scratch.
    allocated_id = allocate_revision_id()
    scratch_path = write_scratch_bytes(allocated_id, root_bytes)
    cache_uri = f"file://{scratch_path}"
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS)

    # 5. POST to Archiver.
    try:
        response = await info_client.post_source_revision(
            info_source_id=resolved.info_source_id,
            content_fingerprint=fingerprint,
            captured_at=now,
            source_revision_id=allocated_id,
            content_cache_uri=cache_uri,
            content_cache_expires_at=expires_at,
            content_size_bytes=len(root_bytes),
            content_media_type=None,
        )
    except Exception as e:
        # Outbox path. Abort cascade (cascade requires root revision to exist).
        await enqueue_pending(
            session,
            info_source_id=resolved.info_source_id,
            content_fingerprint=fingerprint,
            captured_at=now,
            content_cache_uri=cache_uri,
            content_cache_expires_at=expires_at,
            content_size_bytes=len(root_bytes),
            content_media_type=None,
        )
        return {"is_changed": True, "outbox": True, "error": str(e)}

    # 6. Idempotency reconcile (rare: server returned a different ULID).
    canonical_id = str(response.source_revision_id)
    if canonical_id != allocated_id:
        rename_scratch_to_canonical(allocated_id, canonical_id)

    # 7. Update local cache.
    await upsert_last_known(
        session,
        info_source_id=resolved.info_source_id,
        content_fingerprint=fingerprint,
        source_revision_id=canonical_id,
        captured_at=now,
    )

    # 8. Dispatch via existing WatchEvent path.
    effective_url = getattr(watch, "effective_url", None) or resolved.url
    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=effective_url,
        occurred_at=now,
        metadata={
            "source_revision_id": canonical_id,
            "info_source_id": resolved.info_source_id,
            "content_fingerprint": fingerprint,
        },
    )
    await dispatch_event_notifications(session, event)

    result = {
        "is_changed": True,
        "source_revision_id": canonical_id,
        "scratch_path": str(scratch_path_for(canonical_id)),
    }

    # Fragment cascade: extract each child from the same raw_content bytes.
    fragment_revision_ids = []
    for fragment in resolved.children:
        frag_extracted = await _extract_with_spec(raw_content, fragment.source_spec)
        frag_bytes = "\n".join(c.text for c in frag_extracted.chunks).encode()
        frag_fingerprint = "sha256:" + hashlib.sha256(frag_bytes).hexdigest()

        # Per-fragment fast-path.
        prior_frag_fp = await get_last_fingerprint(session, fragment.info_source_id)
        if prior_frag_fp == frag_fingerprint:
            continue

        frag_allocated_id = allocate_revision_id()
        frag_scratch_path = write_scratch_bytes(frag_allocated_id, frag_bytes)
        frag_cache_uri = f"file://{frag_scratch_path}"
        frag_now = datetime.now(UTC)
        frag_expires_at = frag_now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS)
        frag_media_type = getattr(frag_extracted, "media_type", None)

        try:
            frag_response = await info_client.post_source_revision(
                info_source_id=fragment.info_source_id,
                content_fingerprint=frag_fingerprint,
                captured_at=frag_now,
                source_revision_id=frag_allocated_id,
                content_cache_uri=frag_cache_uri,
                content_cache_expires_at=frag_expires_at,
                content_size_bytes=len(frag_bytes),
                content_media_type=frag_media_type,
            )
        except Exception:
            await enqueue_pending(
                session,
                info_source_id=fragment.info_source_id,
                content_fingerprint=frag_fingerprint,
                captured_at=frag_now,
                content_cache_uri=frag_cache_uri,
                content_cache_expires_at=frag_expires_at,
                content_size_bytes=len(frag_bytes),
                content_media_type=frag_media_type,
            )
            continue

        frag_canonical_id = str(frag_response.source_revision_id)
        if frag_canonical_id != frag_allocated_id:
            rename_scratch_to_canonical(frag_allocated_id, frag_canonical_id)

        await upsert_last_known(
            session,
            info_source_id=fragment.info_source_id,
            content_fingerprint=frag_fingerprint,
            source_revision_id=frag_canonical_id,
            captured_at=frag_now,
        )

        # Dispatch per-fragment Watch if one exists.
        frag_watch_q = await session.execute(
            select(Watch)
            .where(Watch.info_source_id == fragment.info_source_id)
            .where(Watch.is_active.is_(True))
            .where(Watch.is_archived.is_(False))
        )
        frag_watch = frag_watch_q.scalar_one_or_none()
        if frag_watch is not None:
            await dispatch_event_notifications(
                session,
                WatchEvent(
                    event_type=WatchEventType.CHANGE_DETECTED,
                    watch_id=str(frag_watch.id),
                    watch_name=frag_watch.name,
                    watch_url=frag_watch.effective_url or resolved.url,
                    occurred_at=frag_now,
                    metadata={
                        "source_revision_id": frag_canonical_id,
                        "info_source_id": fragment.info_source_id,
                        "content_fingerprint": frag_fingerprint,
                        "is_fragment": True,
                        "parent_info_source_id": fragment.parent_info_source_id,
                    },
                ),
            )

        fragment_revision_ids.append(frag_canonical_id)

    result["fragment_revision_ids"] = fragment_revision_ids
    return result
