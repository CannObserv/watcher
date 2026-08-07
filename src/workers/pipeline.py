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

from co_core.pure.extract import (
    ExtractionResult,
    Extractor,
)
from co_core.pure.extract import (
    extraction_config_from_spec as _extraction_config_from_spec,
)
from co_core.pure.extract.html import HtmlExtractor
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.core.media_type import (
    extraction_overrides_for_essence,
    resolve_dispatch_essence,
)
from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.registry import ServiceRegistry, get_registry
from src.core.sources.scratch import write_scratch_bytes
from src.core.utils import watched_item_event_base_metadata

logger = get_logger(__name__)

WATCHER_CACHE_TTL_SECONDS = int(os.environ.get("WATCHER_CACHE_TTL_SECONDS", "600"))


class ExtractionError(Exception):
    """Raised when the dispatched extractor cannot process the fetched bytes.

    Post-#168 the pipeline dispatches PDF/CSV extractors that can raise on bytes
    that don't match the declared/observed type (a mislabeled origin, an HTML
    error page served under a `.pdf` URL, or an operator override mismatch).
    The caller (`check_watched_item`) treats this like a fetch failure so the item
    surfaces a health signal instead of silently re-failing every tick.
    """


# ---------------------------------------------------------------------------
# Extraction helpers.
# ---------------------------------------------------------------------------


def _extract_with_spec(
    raw_content: bytes,
    document: dict,
    *,
    extractor: Extractor | None = None,
    extra_config: dict | None = None,
) -> ExtractionResult:
    """Run an extractor with config derived from a source_spec document.

    Defaults to the HTML extractor (the historical behaviour) when no extractor is
    supplied. ``extra_config`` carries media-type-implied knobs (e.g. the CSV/Excel
    ``content_type`` mode) merged over the spec-derived config. Synchronous:
    co-core extractors are pure (#236).
    """
    extractor = extractor or HtmlExtractor()
    config = _extraction_config_from_spec(document)
    if extra_config:
        config = {**config, **extra_config}
    return extractor.extract(raw_content, config=config)


@dataclass
class ExtractionOutcome:
    """Result of extracting and fingerprinting raw content."""

    content_fingerprint: str
    content_bytes: bytes
    content_size_bytes: int
    schema_version: int


def _extract_and_fingerprint(
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
    media-type-appropriate extractor (defaults to HTML). Synchronous: extraction
    and hashing are pure CPU (#236).
    """
    specs: list[dict] = source_specs if source_specs else [{}]
    result = ExtractionResult(chunks=[])
    used_spec: dict = {}
    for spec in specs:
        result = _extract_with_spec(
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
    errors: list[str] = field(default_factory=list)
    # No archiver_sync_enqueued flag since #251: a detected change always
    # enqueues a PendingArchiverSync row, so `changed` already carries it.


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
    try:
        outcome = _extract_and_fingerprint(
            raw_content, source_specs, extractor=extractor, extra_config=extra_config
        )
    except Exception as exc:
        # PDF/CSV extractors raise on mismatched bytes; surface as a typed error so
        # the caller records a health signal rather than dead-letter-looping (#168).
        raise ExtractionError(f"extraction failed (essence={essence!r}): {exc}") from exc

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

    # Scratch bytes exist only to feed the Archiver sync (drain worker reads them
    # via content_cache_uri). Unconditional since #251: archiver_info_source_id is
    # NOT NULL, so every detected change has somewhere to post. The old guard was
    # the first of two silent-drop branches for bare-URL items — a captured
    # revision was written locally and never enqueued.
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

    watched_item.last_changed_at = now

    # #191: dispatch CHANGE_DETECTED once for the WatchedItem (the monitored entity).
    change_meta: dict = {
        "change_revision_id": str(rev.id),
        "content_fingerprint": outcome.content_fingerprint,
        **watched_item_event_base_metadata(watched_item),
        "archiver_revision_id": None,  # back-filled by drain worker
    }

    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watched_item_id=str(watched_item.id),
        item_name=watched_item.name,
        item_url=watched_item.effective_url,
        occurred_at=now,
        metadata=change_meta,
    )
    await dispatch_event_notifications(session=session, event=event)

    return WatchedItemResult(changed=True, notifications_dispatched=1)
