"""Per-WatchedItem pipeline: fetch once, extract, fingerprint, dispatch.

#185 Phase A. The pipeline reads effective_url and source_specs directly from
the WatchedItem (set at Watch-create time), removing the per-cycle Archiver SDK
call. ChangeRevision rows serve as the local fingerprint history; the first row
is a baseline (no notification); subsequent changes dispatch CHANGE_DETECTED
once for the WatchedItem (the single monitored entity, #191).
"""

import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.extraction_defaults import (
    extraction_config_from_spec as _extraction_config_from_spec,
)
from src.core.extractors import HtmlExtractor
from src.core.extractors.base import ExtractionResult, Extractor
from src.core.logging import get_logger
from src.core.media_type import (
    extraction_overrides_for_essence,
    resolve_dispatch_essence,
)
from src.core.models.change_revision import ChangeRevision
from src.core.models.domain import Domain
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.rate_limiter import DomainRateLimiter
from src.core.registry import ServiceRegistry, get_registry
from src.core.sources.scratch import write_scratch_bytes
from src.core.utils import watched_item_event_base_metadata

logger = get_logger(__name__)

WATCHER_CACHE_TTL_SECONDS = int(os.environ.get("WATCHER_CACHE_TTL_SECONDS", "600"))


# ---------------------------------------------------------------------------
# Backoff helpers — invoked by `check_watched_item` in tasks.py.
# ---------------------------------------------------------------------------


async def _persist_backoff(domain_name: str, new_interval: float, session: AsyncSession) -> None:
    """Persist backoff state to the Domain table after a 429 response."""
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

    Returns True if decay was applied.
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


# ---------------------------------------------------------------------------
# Extraction helpers.
# ---------------------------------------------------------------------------


async def _extract_with_spec(
    raw_content: bytes,
    document: dict,
    *,
    extractor: Extractor | None = None,
    extra_config: dict | None = None,
) -> ExtractionResult:
    """Run an extractor with config derived from a source_spec document.

    Defaults to the HTML extractor (the historical behaviour) when no extractor is
    supplied. ``extra_config`` carries media-type-implied knobs (e.g. the CSV/Excel
    ``content_type`` mode) merged over the spec-derived config.
    """
    extractor = extractor or HtmlExtractor()
    config = _extraction_config_from_spec(document)
    if extra_config:
        config = {**config, **extra_config}
    return await extractor.extract(raw_content, config=config)


@dataclass
class ExtractionOutcome:
    """Result of extracting and fingerprinting raw content."""

    content_fingerprint: str
    content_bytes: bytes
    content_size_bytes: int
    schema_version: int


async def _extract_and_fingerprint(
    raw_content: bytes,
    source_specs: list[dict],
    *,
    extractor: Extractor | None = None,
    extra_config: dict | None = None,
) -> ExtractionOutcome:
    """Extract content and fingerprint, trying source_specs in order until non-empty.

    Falls back to the next spec if the current one yields no chunks. If all
    specs yield empty chunks, uses the last result (fingerprinting empty content
    is a valid baseline). ``extractor``/``extra_config`` select and tune the
    media-type-appropriate extractor (defaults to HTML).
    """
    specs: list[dict] = source_specs if source_specs else [{}]
    result = ExtractionResult(chunks=[])
    used_spec: dict = {}
    for spec in specs:
        result = await _extract_with_spec(
            raw_content, spec, extractor=extractor, extra_config=extra_config
        )
        used_spec = spec
        if result.chunks:
            break
    content_bytes = "\n".join(c.text for c in result.chunks).encode()
    fingerprint = "sha256:" + hashlib.sha256(content_bytes).hexdigest()
    return ExtractionOutcome(
        content_fingerprint=fingerprint,
        content_bytes=content_bytes,
        content_size_bytes=len(content_bytes),
        schema_version=int(used_spec.get("schema_version", 1)),
    )


# ---------------------------------------------------------------------------
# Per-WatchedItem pipeline.
# ---------------------------------------------------------------------------


@dataclass
class WatchedItemResult:
    """Outcome of one check cycle for a WatchedItem."""

    baseline_established: bool = False
    cache_hit: bool = False
    changed: bool = False
    notifications_dispatched: int = 0
    archiver_sync_enqueued: bool = False
    errors: list[str] = field(default_factory=list)


async def process_watched_item(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    raw_content: bytes,
    registry: ServiceRegistry | None = None,
) -> WatchedItemResult:
    """Run one check cycle for a WatchedItem.

    1. Extract content using `watched_item.source_specs`; fingerprint.
    2. Query `change_revisions` for the last fingerprint.
    3. First run: insert baseline ChangeRevision, no notification.
    4. Same fingerprint: cache hit, no action.
    5. Changed: insert new ChangeRevision, optionally enqueue PendingArchiverSync,
       dispatch CHANGE_DETECTED once for the WatchedItem.

    `watched_item.last_changed_at` is updated on change.
    `last_checked_at` and `health_status` are managed by the caller (tasks.py).
    `registry` selects the extractor; defaults to the process singleton. The caller
    (`check_watched_item`) threads its own registry so the extractor and fetcher
    come from the same place (honouring the `ServiceRegistry` injection seam).
    """
    reg = registry if registry is not None else get_registry()
    now = datetime.now(UTC)
    source_specs: list[dict] = watched_item.source_specs or [{}]

    # Dispatch the extractor on the observed/overridden media type (#168 slice 2).
    # Derived in Python from content_media_type (seeded by the caller from this
    # cycle's response header) + a URL-extension tiebreaker; unknown types fall
    # back to the HTML extractor.
    essence = resolve_dispatch_essence(watched_item.content_media_type, watched_item.effective_url)
    extractor = reg.get_extractor(essence)
    extra_config = extraction_overrides_for_essence(essence)
    outcome = await _extract_and_fingerprint(
        raw_content, source_specs, extractor=extractor, extra_config=extra_config
    )

    last_rev = (
        await session.execute(
            select(ChangeRevision)
            .where(ChangeRevision.watched_item_id == watched_item.id)
            .order_by(ChangeRevision.captured_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if last_rev is None:
        # First run: establish baseline — no notification.
        session.add(
            ChangeRevision(
                watched_item_id=watched_item.id,
                content_fingerprint=outcome.content_fingerprint,
                captured_at=now,
                content_size_bytes=outcome.content_size_bytes,
                schema_version=outcome.schema_version,
            )
        )
        return WatchedItemResult(baseline_established=True)

    if last_rev.content_fingerprint == outcome.content_fingerprint:
        return WatchedItemResult(cache_hit=True)

    # Fingerprint changed: insert new ChangeRevision.
    rev = ChangeRevision(
        watched_item_id=watched_item.id,
        content_fingerprint=outcome.content_fingerprint,
        captured_at=now,
        content_size_bytes=outcome.content_size_bytes,
        schema_version=outcome.schema_version,
    )
    session.add(rev)
    await session.flush()  # populate rev.id before scratch write

    # Scratch bytes exist only to feed the Archiver sync (drain worker reads
    # them via content_cache_uri). For un-synced items they are pure local
    # churn — and an orphaned scratch with no pending row, which the sweeper
    # would otherwise 404 on (#194). Gate the write on the same condition.
    archiver_sync_enqueued = False
    if watched_item.archiver_info_source_id:
        scratch_path = write_scratch_bytes(str(rev.id), outcome.content_bytes)
        expires_at = now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS)
        cache_uri = f"file://{scratch_path}"
        session.add(
            PendingArchiverSync(
                change_revision_id=rev.id,
                watched_item_id=watched_item.id,
                content_cache_uri=cache_uri,
                content_cache_expires_at=expires_at,
                next_attempt_at=now,
            )
        )
        archiver_sync_enqueued = True

    watched_item.last_changed_at = now

    # #191: dispatch CHANGE_DETECTED once for the WatchedItem (the monitored entity).
    change_meta: dict = {
        "change_revision_id": str(rev.id),
        "content_fingerprint": outcome.content_fingerprint,
        **watched_item_event_base_metadata(watched_item),
    }
    if archiver_sync_enqueued:
        change_meta["archiver_revision_id"] = None  # back-filled by drain worker

    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watched_item_id=str(watched_item.id),
        item_name=watched_item.name,
        item_url=watched_item.effective_url,
        occurred_at=now,
        metadata=change_meta,
    )
    await dispatch_event_notifications(session=session, event=event)

    return WatchedItemResult(
        changed=True,
        notifications_dispatched=1,
        archiver_sync_enqueued=archiver_sync_enqueued,
    )
