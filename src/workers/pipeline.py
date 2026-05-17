"""Per-WatchedItem pipeline: fetch once, extract per binding, dispatch per Watch.

Phase 6 / Task 7 (#160). Replaces the per-Watch `_run_check_pipeline` with
`process_watched_item`: fetch the InfoItem's primary URL once, extract per
binding (primary + cross_checks + sub_aspects), and dispatch a CHANGE_DETECTED
WatchEvent to each child Watch whose target binding's fingerprint changed in
this cycle.

Cross_check bindings post SourceRevisions (so #157 selector-rot tooling can
read them) but never trigger Watch notifications regardless of fingerprint
movement.
"""

import hashlib
import os
from dataclasses import dataclass, field
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
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.rate_limiter import DomainRateLimiter
from src.core.sources.outbox import enqueue_pending
from src.core.sources.revision_cache import get_last_fingerprint, upsert_last_known
from src.core.sources.scratch import (
    allocate_revision_id,
    rename_scratch_to_canonical,
    write_scratch_bytes,
)
from src.core.watches.info_item_fetch import fetch_info_item_bindings
from src.core.watches.resolution import resolved_schedule_config

logger = get_logger(__name__)

WATCHER_CACHE_TTL_SECONDS = int(os.environ.get("WATCHER_CACHE_TTL_SECONDS", "600"))


# ---------------------------------------------------------------------------
# Backoff helpers — invoked by `check_watched_item` in tasks.py (Task 8).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Extraction helper (preserved — still used per binding).
# ---------------------------------------------------------------------------


async def _extract_with_spec(raw_content: bytes, document: dict) -> ExtractionResult:
    """Run the HTML extractor with config derived from the InfoSource source_spec document.

    Phase 2c only supports HTML. PDF + FILE return in Phase 3+ once the
    InfoSource schema gains the corresponding extraction algorithms.
    """
    extractor = HtmlExtractor()
    config = _extraction_config_from_spec(document)
    return await extractor.extract(raw_content, config=config)


# ---------------------------------------------------------------------------
# Per-WatchedItem pipeline (Phase 6 / Task 7).
# ---------------------------------------------------------------------------


@dataclass
class BindingOutcome:
    """Outcome of processing one InfoSource binding within a cycle."""

    info_source_id: str
    posted: bool = False
    enqueued: bool = False
    cache_hit: bool = False
    source_revision_id: str | None = None
    content_fingerprint: str | None = None
    error: str | None = None


@dataclass
class WatchedItemResult:
    """Per-binding outcomes + dispatch counts for a single WatchedItem cycle.

    `check_watched_item` (Task 8) uses these to update last_checked_at on every
    Watch and last_changed_at on Watches whose target binding changed.
    """

    bindings_processed: int = 0
    revisions_posted: int = 0
    revisions_enqueued: int = 0
    cache_hits: int = 0
    notifications_dispatched: int = 0
    changed_info_source_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _spec_dict(spec_obj: object) -> dict:
    """Coerce a binding's `source_spec` into a plain dict.

    The Archiver SDK returns the spec as an `UNSET`-aware object exposing
    `additional_properties` (the JSONB document). The mock client in tests
    follows the same shape.
    """
    if hasattr(spec_obj, "additional_properties"):
        return dict(spec_obj.additional_properties)  # type: ignore[attr-defined]
    if hasattr(spec_obj, "to_dict"):
        return dict(spec_obj.to_dict())  # type: ignore[attr-defined]
    if isinstance(spec_obj, dict):
        return dict(spec_obj)
    return {}


async def _process_binding(
    session: AsyncSession,
    info_client: ArchiverClient,
    binding: object,
    raw_content: bytes,
    now: datetime,
) -> BindingOutcome:
    """Extract → fingerprint → fast-path → POST/enqueue → cache for one binding.

    Returns the per-binding outcome. Caller decides whether the change should
    trigger a Watch notification (cross_check bindings never do).
    """
    info_source_id = str(binding.info_source_id)  # type: ignore[attr-defined]
    outcome = BindingOutcome(info_source_id=info_source_id)

    document = _spec_dict(binding.source_spec)  # type: ignore[attr-defined]
    extracted = await _extract_with_spec(raw_content, document)
    content_bytes = "\n".join(c.text for c in extracted.chunks).encode()
    fingerprint = "sha256:" + hashlib.sha256(content_bytes).hexdigest()
    outcome.content_fingerprint = fingerprint

    prior_fp = await get_last_fingerprint(session, info_source_id)
    if prior_fp == fingerprint:
        outcome.cache_hit = True
        return outcome

    allocated_id = allocate_revision_id()
    scratch_path = write_scratch_bytes(allocated_id, content_bytes)
    cache_uri = f"file://{scratch_path}"
    expires_at = now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS)
    media_type = getattr(extracted, "media_type", None)

    try:
        response = await info_client.post_source_revision(
            info_source_id=info_source_id,
            content_fingerprint=fingerprint,
            captured_at=now,
            source_revision_id=allocated_id,
            content_cache_uri=cache_uri,
            content_cache_expires_at=expires_at,
            content_size_bytes=len(content_bytes),
            content_media_type=media_type,
        )
    except Exception as e:
        # Outbox path — Archiver unreachable. Notification will fire from the
        # drain worker once the row is acked (Task 8b).
        await enqueue_pending(
            session,
            info_source_id=info_source_id,
            content_fingerprint=fingerprint,
            captured_at=now,
            content_cache_uri=cache_uri,
            content_cache_expires_at=expires_at,
            content_size_bytes=len(content_bytes),
            content_media_type=media_type,
        )
        outcome.enqueued = True
        outcome.error = str(e)
        return outcome

    canonical_id = str(response.source_revision_id)
    if canonical_id != allocated_id:
        rename_scratch_to_canonical(allocated_id, canonical_id)

    await upsert_last_known(
        session,
        info_source_id=info_source_id,
        content_fingerprint=fingerprint,
        source_revision_id=canonical_id,
        captured_at=now,
    )

    outcome.posted = True
    outcome.source_revision_id = canonical_id
    return outcome


async def process_watched_item(
    session: AsyncSession,
    info_client: ArchiverClient,
    watched_item: WatchedItem,
    *,
    raw_content: bytes,
) -> WatchedItemResult:
    """Run one check cycle for a WatchedItem.

    1. Resolve the InfoItem's bindings via Archiver (primary + cross_checks + sub_aspects).
    2. For each binding, extract content, fingerprint, fast-path against the
       local revision cache, and POST/enqueue if changed.
    3. Load this WatchedItem's active+non-archived child Watches; for each
       Watch dispatch a CHANGE_DETECTED event iff its target binding's
       fingerprint changed in this cycle. ``target_info_source_id IS NULL``
       points at the primary binding; non-NULL points at a specific
       sub_aspect.

    Cross_check bindings post SourceRevisions but never produce a Watch
    notification — they are selector-rot infrastructure (#157).

    Watches whose ``target_info_source_id`` no longer matches any active
    sub_aspect binding are logged and skipped (Archiver may have deactivated
    the binding between cycles).

    Returns per-cycle outcomes so the caller (Task 8 ``check_watched_item``)
    can update last_checked_at / last_changed_at on child Watches.
    """
    result = WatchedItemResult()
    bindings = await fetch_info_item_bindings(info_client, str(watched_item.info_item_id))
    now = datetime.now(UTC)

    primary_source_id = str(bindings.primary.info_source_id)  # type: ignore[attr-defined]
    all_bindings = [bindings.primary, *bindings.cross_checks, *bindings.sub_aspects]
    cross_check_ids = {
        str(b.info_source_id)  # type: ignore[attr-defined]
        for b in bindings.cross_checks
    }
    sub_aspect_ids = {
        str(b.info_source_id)  # type: ignore[attr-defined]
        for b in bindings.sub_aspects
    }

    # Process each binding and track which fingerprints actually changed
    # (Archiver-acked). Outbox-only changes do not fire inline notifications.
    outcomes: dict[str, BindingOutcome] = {}
    for binding in all_bindings:
        outcome = await _process_binding(session, info_client, binding, raw_content, now)
        outcomes[outcome.info_source_id] = outcome
        result.bindings_processed += 1
        if outcome.posted:
            result.revisions_posted += 1
            result.changed_info_source_ids.append(outcome.info_source_id)
        if outcome.enqueued:
            result.revisions_enqueued += 1
            if outcome.error:
                result.errors.append(outcome.error)
        if outcome.cache_hit:
            result.cache_hits += 1

    # Load child Watches and dispatch per-Watch if its target binding changed.
    watches = (
        (
            await session.execute(
                select(Watch)
                .where(Watch.watched_item_id == watched_item.id)
                .where(Watch.is_active.is_(True))
                .where(Watch.is_archived.is_(False))
            )
        )
        .scalars()
        .all()
    )

    for watch in watches:
        if watch.target_info_source_id is None:
            target_id = primary_source_id
        else:
            target_id = str(watch.target_info_source_id)
            if target_id in cross_check_ids:
                # Defence-in-depth: Watch should never point at a cross_check
                # (#160 invariant), but if one slipped through we never
                # dispatch from this surface.
                logger.warning(
                    "Watch %s targets cross_check binding %s; skipping dispatch",
                    watch.id,
                    target_id,
                )
                continue
            if target_id not in sub_aspect_ids:
                logger.warning(
                    "Watch %s targets info_source_id=%s which is no longer a "
                    "sub_aspect of InfoItem %s; skipping",
                    watch.id,
                    target_id,
                    watched_item.info_item_id,
                )
                continue

        target_outcome = outcomes.get(target_id)
        if target_outcome is None or not target_outcome.posted:
            # Either no binding (shouldn't happen given the checks above) or
            # the fingerprint didn't change / outbox-only this cycle.
            continue

        change_meta: dict = {
            "source_revision_id": target_outcome.source_revision_id,
            "info_source_id": target_id,
            "content_fingerprint": target_outcome.content_fingerprint,
        }
        if watch.effective_domain:
            change_meta["effective_domain"] = watch.effective_domain
        interval = resolved_schedule_config(watch).get("interval")
        if interval:
            change_meta["check_interval"] = interval
        if watch.tags:
            change_meta["tags"] = watch.tags
        if watch.description:
            change_meta["description"] = watch.description
        if watch.target_info_source_id is not None:
            change_meta["is_fragment"] = True
            change_meta["parent_info_source_id"] = primary_source_id

        event = WatchEvent(
            event_type=WatchEventType.CHANGE_DETECTED,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=watch.effective_url or bindings.primary_url,
            occurred_at=now,
            metadata=change_meta,
        )
        await dispatch_event_notifications(session, event)
        result.notifications_dispatched += 1
        watch.last_changed_at = now

    return result
