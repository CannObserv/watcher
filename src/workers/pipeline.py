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
    spec_fingerprint,
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

# The media type of the EXTRACTED content, not of what the origin served. Every
# extractor in the registry produces text, joined and UTF-8 encoded; the wire
# keeps this and ``source_media_type`` as separate fields precisely because they
# differ for one revision (an HTML page is served text/html; the text extracted
# from it is not).
EXTRACTED_CONTENT_MEDIA_TYPE = "text/plain; charset=utf-8"


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


@dataclass(frozen=True)
class BlobProvenance:
    """The correlated ``content.blobs`` fact, carried onto the outbox row (#253).

    Supplied by the apply path, which holds the ``FetchCommand`` when it calls
    the pipeline. Optional here only while the HTTP POST path still drains off
    ``content_cache_uri``; the publisher requires it, so this tightens to a
    required argument when the POST goes away.

    Every field is nullable because its source column is (``fetch_commands`` fact
    fields are all populated by the consumer, so they are NULL until the fact
    lands). In practice a row that reaches apply has read its blob, so
    ``blob_uri`` is set, and ``media_type`` is required on ``BlobAvailableEvent``
    — but a dataclass validates nothing, and declaring ``str`` while a ``None``
    flows through would move the failure from the publisher's dead-letter path,
    where it is classified, to a type annotation nobody enforces (CR-2).
    """

    command_id: str
    blob_uri: str | None
    source_media_type: str | None
    blob_expires_at: datetime | None = None


@dataclass
class ExtractionOutcome:
    """Result of extracting and fingerprinting raw content."""

    content_fingerprint: str
    content_bytes: bytes
    content_size_bytes: int
    schema_version: int
    # Identity of the spec the fallback loop actually bound — per-spec, so a
    # fallback from spec[0] to spec[1] moves it (cannobserv#309). ``None`` when
    # co-core cannot derive one; a diagnostic must never fail the pipeline.
    spec_fingerprint: str | None = None
    content_media_type: str = EXTRACTED_CONTENT_MEDIA_TYPE


def _extract_and_fingerprint(
    raw_content: bytes,
    source_specs: list[dict],
    *,
    extractor: Extractor | None = None,
    extra_config: dict | None = None,
    spec_id: str | None = None,
) -> ExtractionOutcome:
    """Extract content and fingerprint, trying source_specs in order until non-empty.

    Falls back to the next spec if the current one yields no chunks. If all
    specs yield empty chunks, uses the last result — reporting what it found
    rather than judging it; the caller rejects the all-empty outcome (#258).
    ``extractor``/``extra_config`` select and tune the
    media-type-appropriate extractor (defaults to HTML). Synchronous: extraction
    and hashing are pure CPU (#236).

    ``spec_fingerprint`` is ``None`` when the item carried no ``source_specs``:
    the ``[{}]`` below is a synthetic full-page default, not a spec anyone
    authored, and ``spec_fingerprint({})`` would name one present in no registry
    — Archiver's index lookup would miss and flag the revision as superseded.
    ``spec_id`` identifies the item for the warning path (#253 CR-7).
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
        spec_fingerprint=(
            _spec_fingerprint_or_none(used_spec, spec_id=spec_id) if source_specs else None
        ),
    )


def _spec_fingerprint_or_none(spec: dict, *, spec_id: str | None = None) -> str | None:
    """co-core's derivation over the spec that produced the bytes, or ``None``.

    ``SpecFingerprintError`` subclasses ``ValueError``; co-core rejects a spec
    carrying a float, an explicit null, a non-ASCII key, or any non-JSON type.
    Every one of those is a reason to report no spec identity rather than to
    lose the revision — the field is a diagnostic, and Archiver's policy is
    record-and-flag, never reject (archiver#139).

    ``spec_id`` names the WatchedItem in the warning: an operator who learns only
    that *a* spec somewhere is malformed cannot go fix one (#253 CR-7).
    """
    try:
        return spec_fingerprint(spec)
    except ValueError as exc:
        logger.warning(
            "spec_fingerprint underivable",
            extra={"error": str(exc), "watched_item_id": spec_id},
        )
        return None


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
    blob: BlobProvenance | None = None,
) -> WatchedItemResult:
    """Run one check cycle for a WatchedItem.

    1. Extract content using `watched_item.source_specs`; fingerprint.
    2. Empty extraction: raise `ExtractionError` — never a revision (#258).
    3. Query `change_revisions` for the last fingerprint.
    4. First run: insert baseline ChangeRevision, no notification.
    5. Same fingerprint: cache hit, no action.
    6. Changed: insert new ChangeRevision, optionally enqueue PendingArchiverSync,
       dispatch CHANGE_DETECTED once for the WatchedItem.

    `watched_item.last_changed_at` is updated on change.
    `last_checked_at` and `health_status` are managed by the caller (tasks.py).
    `registry` selects the extractor; defaults to the process singleton. The caller
    (`check_watched_item`) threads its own registry so the extractor and fetcher
    come from the same place (honouring the `ServiceRegistry` injection seam).
    `blob` carries the correlated `content.blobs` fact onto the outbox row (#253);
    the apply path always supplies it, and the publisher requires it.
    """
    reg = registry if registry is not None else get_registry()
    now = datetime.now(UTC)
    # Passed through as-is, empty included: substituting the synthetic ``[{}]``
    # here would hide "the item has no specs" from _extract_and_fingerprint,
    # which is exactly the distinction spec_fingerprint must report (CR-1). The
    # extractor's own default handles the empty list.
    source_specs: list[dict] = watched_item.source_specs or []

    # Dispatch the extractor on the observed/overridden media type (#168 slice 2).
    # Derived in Python from content_media_type (seeded by the caller from this
    # cycle's response header) + a URL-extension tiebreaker; unknown types fall
    # back to the HTML extractor.
    essence = resolve_dispatch_essence(watched_item.content_media_type, watched_item.effective_url)
    extractor = reg.get_extractor(essence)
    extra_config = extraction_overrides_for_essence(essence)
    try:
        outcome = _extract_and_fingerprint(
            raw_content,
            source_specs,
            extractor=extractor,
            extra_config=extra_config,
            spec_id=str(watched_item.id),
        )
    except Exception as exc:
        # PDF/CSV extractors raise on mismatched bytes; surface as a typed error so
        # the caller records a health signal rather than dead-letter-looping (#168).
        raise ExtractionError(f"extraction failed (essence={essence!r}): {exc}") from exc

    # #258: every spec yielded empty. Not an exception from the extractor, but not
    # a content observation either — the fallback loop exhausted, and its terminal
    # case left `used_spec` pointing at the last spec rather than a chosen one.
    # Treated as a failure unconditionally, on both sides of a baseline: an item
    # whose extraction yields nothing is a broken watch either way, and the
    # alternative is worse in the silent direction. Empty content fingerprints
    # consistently, so before this guard rot presented as a *content change* —
    # zero-byte revision, POST to Archiver, CHANGE_DETECTED notification, health
    # still OK — and an item broken from its first check baselined on the empty
    # digest and never reported anything again.
    if outcome.content_size_bytes == 0:
        raise ExtractionError(
            f"every source_spec yielded empty content (essence={essence!r}, "
            f"specs={len(source_specs)})"
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

    # Scratch bytes exist only to feed the Archiver sync (drain worker reads them
    # via content_cache_uri). Unconditional since #251: archiver_info_source_id is
    # NOT NULL, so every detected change has somewhere to post. The old guard was
    # the first of two silent-drop branches for bare-URL items — a captured
    # revision was written locally and never enqueued.
    scratch_path = write_scratch_bytes(str(rev.id), outcome.content_bytes)
    expires_at = now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS)
    cache_uri = f"file://{scratch_path}"
    # The outbox row is the observation, so it carries where the bytes came from
    # (#253): the blob facts as Replicator stated them, plus the identity of the
    # spec they were extracted under. Snapshotted here rather than joined at
    # drain time — the FetchCommand's lifecycle is not the outbox row's, and the
    # values are free at this point because the apply path already holds them.
    session.add(
        PendingArchiverSync(
            change_revision_id=rev.id,
            watched_item_id=watched_item.id,
            content_cache_uri=cache_uri,
            content_cache_expires_at=expires_at,
            next_attempt_at=now,
            command_id=blob.command_id if blob else None,
            blob_uri=blob.blob_uri if blob else None,
            blob_expires_at=blob.blob_expires_at if blob else None,
            source_media_type=blob.source_media_type if blob else None,
            content_media_type=outcome.content_media_type,
            spec_fingerprint=outcome.spec_fingerprint,
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
