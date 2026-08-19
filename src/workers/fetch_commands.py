"""Phase-4 fetch-command worker tasks (#241): sweep, apply, failure, reaper.

* ``publish_pending_fetch_commands`` — the second half of persist-before-publish
  (issuer contract MUST-2): a crash or broker outage between a row's commit and
  its XADD leaves ``pending_publish``, republished **under the same
  ``command_id``** (idempotent by Replicator's dedupe). Every minute; no-op
  query when idle.
* ``apply_fetch_blob`` / ``apply_fetch_failure`` / ``apply_fetch_not_modified``
  — deferred by the content.blobs consumer (``src/workers/fetch_facts.py``)
  after it upserts a fact; they restore the exact bookkeeping the local fetch
  path performs (health, ``last_checked_at``, audits, WATCH_ERROR/RECOVERED).
  Two of the three are success paths: a 304 is a check that found no change, not
  a failed check (#249). An unreadable blob re-issues, capped — every turn of
  that loop is a real origin request (#275).
* ``reap_fetch_commands`` — MUST-6's backstop for the still-silent outcomes
  (stalls, undecodable frames): expire + re-issue with intent lineage, cap with
  an ERROR surface.
"""

from datetime import UTC, datetime, timedelta

import procrastinate
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.blobs import BlobUnreadable, UnsupportedBlobScheme, aread_blob
from src.core.bus import BUS_REDIS_URL_ENV, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.domains import domain_name_for_url, ensure_domain_and_resolve_suspension
from src.core.fetch_commands import (
    create_fetch_command,
    fetch_command_timeout_seconds,
    fetch_max_reissues,
    publish_fetch_command,
    select_pending_publish,
)
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.fetch_command import (
    BLOB_UNREADABLE_REASON,
    INVALID_REQUEST_OPTIONS_REASON,
    FetchCommand,
    FetchCommandStatus,
)
from src.core.models.watched_item import (
    CONTENT_MEDIA_TYPE_MAX_LEN,
    WatchedItem,
    WatchHealthStatus,
)
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.registry import ServiceRegistry, get_registry
from src.core.utils import watched_item_event_base_metadata
from src.core.validators import clear_validators, record_validators, stamp_full_fetch
from src.workers import bp
from src.workers.notify import dispatch_event_notifications
from src.workers.pipeline import (
    BlobProvenance,
    ExtractionError,
    WatchedItemResult,
    process_watched_item,
)
from src.workers.watch_status import defer_status_republish

logger = get_logger(__name__)


async def _record_check_failure(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    now: datetime,
    url: str,
    audit_event: str,
    audit_kwargs: dict,
    error_metadata: dict,
) -> None:
    """Record a failed check: ERROR health + stamped ``last_checked_at`` + audit,
    and dispatch ``WATCH_ERROR`` once on the OK→ERROR transition.

    Shared by the fetch-failure and extraction-failure paths so both surface a
    health signal and a fresh ``last_checked_at`` — the latter stops a persistent
    failure from being re-enqueued every ``schedule_tick`` (#168).
    """
    audit(session, audit_event, watched_item_id=str(watched_item.id), **audit_kwargs)
    previous_health = watched_item.health_status
    watched_item.health_status = WatchHealthStatus.ERROR
    watched_item.last_checked_at = now
    # last_observed_at deliberately NOT stamped: a failure is an attempt, never
    # an observation (#264) — the registry must not claim content was verified.
    await session.commit()

    if previous_health != WatchHealthStatus.ERROR:
        # Health transition: level signal changed, so the watch-status stream
        # republishes (#264). Post-commit, best-effort; steady-state failures
        # never publish — that keeps the stream off the activity-rate curve.
        await defer_status_republish()

    if previous_health != WatchHealthStatus.ERROR:
        error_event = WatchEvent(
            event_type=WatchEventType.WATCH_ERROR,
            watched_item_id=str(watched_item.id),
            item_name=watched_item.name,
            item_url=watched_item.effective_url or url,
            occurred_at=now,
            metadata={**error_metadata, **watched_item_event_base_metadata(watched_item)},
        )
        await dispatch_event_notifications(session=session, event=error_event)
        await session.commit()


async def _record_check_success(
    session: AsyncSession,
    watched_item: WatchedItem,
    result,
    *,
    now: datetime,
    url: str,
    audit_extra: dict | None = None,
) -> None:
    """Record a successful check: audit trail + OK health + ``last_checked_at``,
    and dispatch ``WATCH_RECOVERED`` once on the ERROR→OK transition.

    Shared by the succeeding apply paths (``apply_fetch_blob`` /
    ``apply_fetch_not_modified``) so every outcome leaves identical bookkeeping —
    the dashboard checks_today stat and WatchedItem activity read these events. A
    snapshot event marks a baseline/changed cycle (a ChangeRevision was
    written); otherwise the content was unchanged.

    ``audit_extra`` adds payload keys to that audit without changing which event
    fires — the 304 path uses it to stay distinguishable from an unchanged
    *extraction*, which is a materially different observation (#249).
    """
    snapshot = result.baseline_established or result.changed
    audit(
        session,
        EventType.CHECK_SNAPSHOT_CREATED if snapshot else EventType.CHECK_NO_CHANGE,
        watched_item_id=str(watched_item.id),
        changed=result.changed,
        baseline=result.baseline_established,
        **(audit_extra or {}),
    )

    previous_health = watched_item.health_status
    watched_item.health_status = WatchHealthStatus.OK
    watched_item.last_checked_at = now
    # Observation freshness (#264): "content was verified current", where
    # last_checked_at only says "we tried". Both callers qualify — a successful
    # extraction (changed or unchanged alike), and a 304 in which the origin
    # itself asserts the bytes are current without sending any (#249). The
    # audit's `source` key is what keeps those two apart downstream.
    # Published on info.watch-status; Archiver records it durably.
    watched_item.last_observed_at = now
    await session.commit()

    if previous_health != WatchHealthStatus.OK:
        # Health transition (#264): post-commit, best-effort. A steady OK cycle
        # never publishes — its timestamps converge at the periodic republish.
        await defer_status_republish()

    # Recovery: dispatch WATCH_RECOVERED once when the WatchedItem
    # transitions ERROR → OK (#191).
    if previous_health == WatchHealthStatus.ERROR:
        recovery_event = WatchEvent(
            event_type=WatchEventType.WATCH_RECOVERED,
            watched_item_id=str(watched_item.id),
            item_name=watched_item.name,
            item_url=watched_item.effective_url or url,
            occurred_at=now,
            metadata=watched_item_event_base_metadata(watched_item),
        )
        await dispatch_event_notifications(session=session, event=recovery_event)
        await session.commit()


# Transient-infra retry for the apply tasks (CR-2): DB restarts
# (OperationalError), broker/notifier blips. Mirrors check_watched_item's
# shape; permanent errors (bugs) still fail the job — the reaper's re-defer
# is the last-resort resurrection for those.
_APPLY_RETRY = procrastinate.RetryStrategy(
    max_attempts=3,
    exponential_wait=5,
    retry_exceptions={
        ConnectionError,
        TimeoutError,
        RedisConnectionError,
        RedisTimeoutError,
        OperationalError,
    },
)


@bp.periodic(cron="* * * * *", periodic_id="publish_pending_fetch_commands")
@bp.task(name="publish_pending_fetch_commands", queue="default")
async def publish_pending_fetch_commands(
    *, session=None, bus_client=None, batch_size: int = 100, **periodic_kwargs
) -> dict:
    """Republish every ``pending_publish`` fetch command (same id — dedupe-safe).

    ``session`` / ``bus_client`` are test seams; production opens its own. A
    per-row failure is logged and the row stays pending for the next tick. The
    missing-env error is raised only when there is actually work to publish —
    an idle sweep in a bus-less deployment must not spam the journal.
    """
    owns_session = session is None
    ctx = get_session_factory()() if owns_session else None
    db = await ctx.__aenter__() if owns_session else session
    try:
        rows = await select_pending_publish(db, limit=batch_size)
        if not rows:
            return {"published": 0}

        # Shared, lifespan-owned client (CR-4) — never closed here.
        client = bus_client if bus_client is not None else get_shared_bus_client()
        if client is None:
            logger.error(
                "cannot republish %d pending fetch command(s): %s is not set",
                len(rows),
                BUS_REDIS_URL_ENV,
            )
            return {"published": 0, "skipped": f"{BUS_REDIS_URL_ENV} not set"}

        published = 0
        for row in rows:
            try:
                await publish_fetch_command(client, row)
                await db.commit()
                published += 1
            except Exception:
                # No rollback: publish raises before any ORM mutation, so
                # the session is clean; a (rare) failed commit poisons only
                # this task run and the next tick gets a fresh session.
                logger.warning(
                    "fetch command republish failed; will retry next tick",
                    extra={"command_id": row.command_id},
                    exc_info=True,
                )
        if published:
            logger.info("republished pending fetch commands", extra={"published": published})
        return {"published": published}
    finally:
        if owns_session:
            await ctx.__aexit__(None, None, None)


async def _reissue(session, watched_item, prior, client) -> str:
    """Re-issue an intent under a fresh ``command_id`` (MUST-6: a timeout or a
    lost blob is grounds to re-issue, never to conclude failure).

    Same ``intent_id``, ``reissue_count + 1``, same forced-fetch intent.
    Persist-commit-publish, like the original issue; a failed publish leaves
    ``pending_publish`` for the sweep.
    Returns the new ``command_id``.
    """
    now = datetime.now(UTC)
    row = await create_fetch_command(
        session,
        watched_item,
        now=now,
        intent_id=prior.intent_id,
        reissue_count=prior.reissue_count + 1,
        # The forced intent is lineage too (CR-1): a check-now that stalled and
        # was reaped must not come back as a conditional GET the origin can
        # answer 304, leaving the operator with no bytes and no signal.
        force_full_fetch=prior.forced_full_fetch,
    )
    await session.commit()
    # Shared, lifespan-owned client (CR-4) — never closed here.
    publish_client = client if client is not None else get_shared_bus_client()
    if publish_client is None:
        logger.error("re-issued fetch command cannot publish: %s is not set", BUS_REDIS_URL_ENV)
        return row.command_id
    try:
        await publish_fetch_command(publish_client, row, now=now)
        await session.commit()
    except Exception:
        logger.warning(
            "re-issue publish failed; the sweep will retry it",
            extra={"command_id": row.command_id},
            exc_info=True,
        )
    return row.command_id


async def _fail_blob_unreadable(session, watched_item, row, *, now: datetime, detail: str) -> dict:
    """Terminate an intent whose bytes never became readable (#275).

    The same shape the reaper's cap uses — FAILED + ERROR health + a fresh
    ``last_checked_at``, so the item leaves ``OPEN_STATUSES`` and re-enters
    normal scheduling; recovery is automatic once the cause is fixed — under its
    own ``failure_reason``.

    Neither validator helper fires here. No bytes arrived, so ``stamp_full_fetch``
    would be a lie (CR-13); and being unable to *read* a blob says nothing about
    the stored pair, so it survives exactly as it does under every
    ``apply_fetch_failure`` reason but ``invalid_request_options``.
    """
    row.status = FetchCommandStatus.FAILED
    row.failure_reason = BLOB_UNREADABLE_REASON
    row.failure_detail = detail
    row.applied_at = now
    await _record_check_failure(
        session,
        watched_item,
        now=now,
        url=row.url,
        audit_event=EventType.CHECK_FETCH_FAILED,
        audit_kwargs={"reason": BLOB_UNREADABLE_REASON, "reissues": row.reissue_count},
        error_metadata={"reason": BLOB_UNREADABLE_REASON},
    )
    return {"error": BLOB_UNREADABLE_REASON, "reissues": row.reissue_count}


@bp.task(
    name="apply_fetch_blob",
    queue="default",
    # CR-2: a transient infra error must not strand the row IN_FLIGHT with its
    # fact recorded — retry here first; the reaper's re-defer is the backstop.
    retry=_APPLY_RETRY,
)
async def apply_fetch_blob(
    command_id: str, registry: ServiceRegistry | None = None, bus_client=None
) -> dict:
    """Run the check pipeline over a returned blob (#241 apply path).

    Deferred by the content.blobs consumer after it upserts the fact onto the
    ``fetch_commands`` row. Applies at most once per command (status guard —
    duplicate facts re-defer this task, MUST-4), and never applies out of order:
    if a newer command for the item has already applied, this one is recorded
    ``superseded`` (the bus guarantees no ordering; an A→B→A fingerprint flap
    would fire a phantom CHANGE_DETECTED).

    A blob that cannot be read (reaped, or a cross-host ``blob_uri``) is a
    re-issue under a fresh ``command_id`` — MUST-7's clock runs from last fetch,
    not last read, so waiting cannot help — but **capped** at
    ``WATCHER_FETCH_MAX_REISSUES``, the same lineage counter the reaper caps
    (#275). A backend this build cannot read at all skips the re-issues
    entirely: see ``_fail_blob_unreadable``.
    """
    reg = registry if registry is not None else get_registry()
    async with get_session_factory()() as session:
        row = await session.get(FetchCommand, command_id)
        if row is None:
            logger.warning("apply: unknown fetch command", extra={"command_id": command_id})
            return {"skipped": True, "reason": "unknown_command"}
        if row.status != FetchCommandStatus.IN_FLIGHT:
            return {"skipped": True, "reason": f"status_{row.status}"}
        watched_item = await session.get(WatchedItem, row.watched_item_id)
        if watched_item is None:
            row.status = FetchCommandStatus.SUPERSEDED
            await session.commit()
            return {"skipped": True, "reason": "watched_item_gone"}

        now = datetime.now(UTC)

        # Ordering guard: apply only if nothing newer has applied for this item.
        newest_applied = (
            await session.execute(
                select(func.max(FetchCommand.issued_at)).where(
                    FetchCommand.watched_item_id == row.watched_item_id,
                    FetchCommand.applied_at.is_not(None),
                )
            )
        ).scalar_one_or_none()
        if newest_applied is not None and newest_applied > row.issued_at:
            row.status = FetchCommandStatus.SUPERSEDED
            await session.commit()
            return {"skipped": True, "reason": "superseded"}

        try:
            # Off the event loop (CR-2): this task shares its process with the
            # API and the content.blobs consumer, and a blob is not small.
            raw_content = await aread_blob(row.blob_uri)
        except UnsupportedBlobScheme as exc:
            # Deterministic (#275): a re-issue's fact would name the same
            # backend, so each turn of the loop is a real origin request spent
            # learning the same thing. Terminal on the first occasion.
            logger.error(
                "blob_uri names an unreadable backend — failing the intent",
                extra={"command_id": command_id, "blob_uri": row.blob_uri, "error": str(exc)},
            )
            return await _fail_blob_unreadable(session, watched_item, row, now=now, detail=str(exc))
        except BlobUnreadable as exc:
            # May be transient (blob reaped between fact and apply), so re-issue
            # — but under the SAME lineage cap the reaper applies to stalls
            # (#275). Without it a systematic cause (a permissions change under
            # the blob dir, a cross-host blob_uri) loops forever, each turn a
            # real origin fetch, with health still reading OK.
            if row.reissue_count >= fetch_max_reissues():
                logger.error(
                    "blob still unreadable at the re-issue cap — failing the intent",
                    extra={
                        "command_id": command_id,
                        "blob_uri": row.blob_uri,
                        "reissues": row.reissue_count,
                        "error": str(exc),
                    },
                )
                return await _fail_blob_unreadable(
                    session, watched_item, row, now=now, detail=str(exc)
                )
            logger.warning(
                "blob unreadable — re-issuing the intent",
                extra={"command_id": command_id, "blob_uri": row.blob_uri, "error": str(exc)},
            )
            row.status = FetchCommandStatus.EXPIRED
            new_id = await _reissue(session, watched_item, row, bus_client)
            return {"reissued": new_id}

        # Async-probe resolution (#241 step 3): a PROBING item's first fact is
        # its probe. When the origin redirected, final_url becomes the
        # effective_url — exactly what the inline probe used to discover — and
        # the domain facts are re-derived through the shared #196 helper (which
        # also retires any fetch-policy tombstone for the new host). PROBING
        # clears to OK via _record_check_success below. Steady-state redirects
        # (non-PROBING) stay audit-only: Archiver is authoritative then.
        if (
            watched_item.health_status == WatchHealthStatus.PROBING
            and row.final_url
            and row.final_url != watched_item.effective_url
        ):
            new_domain = domain_name_for_url(row.final_url)
            # Upsert the Domain row before the item references it (FK ordering:
            # ensure_domain's nested commit flushes the dirty item too).
            resolution = await ensure_domain_and_resolve_suspension(session, new_domain)
            watched_item.effective_url = row.final_url
            watched_item.domain_name = new_domain
            watched_item.domain_suspended = resolution.suspended
            watched_item.domain_default_schedule_config = resolution.default_schedule_config
            audit(
                session,
                EventType.WATCHED_ITEM_UPDATED,
                watched_item_id=str(watched_item.id),
                updated_fields=["effective_url", "domain_name"],
                source="probe_resolution",
            )

        # Seed the observed media type once, from the RAW header (#168 semantics:
        # an absent header is not application/octet-stream; the consumer stored
        # None for absent, so the seed-once rule survives the cutover).
        if row.content_type_raw and not watched_item.content_media_type:
            watched_item.content_media_type = row.content_type_raw[:CONTENT_MEDIA_TYPE_MAX_LEN]

        try:
            result = await process_watched_item(
                session=session,
                watched_item=watched_item,
                raw_content=raw_content,
                registry=reg,
                # Both nullable columns, carried through as-is: media_type is
                # required on the blob fact and the blob has already been read
                # by here, so in practice both are set — but "" would be a lie
                # and the publisher's dead-letter path is where a missing value
                # belongs, not a silent substitution here (CR-2).
                blob=BlobProvenance(
                    command_id=row.command_id,
                    blob_uri=row.blob_uri,
                    source_media_type=row.media_type,
                    blob_expires_at=row.blob_expires_at,
                ),
            )
        except ExtractionError as exc:
            logger.warning(
                "extraction failed on applied blob",
                extra={"command_id": command_id, "error": str(exc)},
            )
            row.status = FetchCommandStatus.FAILED
            row.applied_at = now
            # #269: bytes arrived and could not be extracted. Keeping the stored
            # pair would let the next cycle answer 304 — and a 304 apply records
            # a *successful* check, so a broken extraction would flip back to OK
            # health without anything having been extracted. Forget it, and the
            # next command fetches in full and re-asserts the failure.
            clear_validators(watched_item)
            # …but bytes DID arrive, and that is what this stamp records (CR-2).
            stamp_full_fetch(watched_item, now=now)
            await _record_check_failure(
                session,
                watched_item,
                now=now,
                url=row.url,
                audit_event=EventType.CHECK_EXTRACTION_FAILED,
                audit_kwargs={"error": str(exc)},
                error_metadata={"error": "extraction_failed"},
            )
            return {"error": "extraction_failed"}

        # #157 breadcrumb: the origin redirected. Audit only — Archiver stays
        # authoritative for effective_url.
        if row.final_url and row.final_url != row.url:
            audit(
                session,
                EventType.CHECK_REDIRECT_OBSERVED,
                watched_item_id=str(watched_item.id),
                requested_url=row.url,
                final_url=row.final_url,
            )

        row.status = FetchCommandStatus.SUCCEEDED
        row.applied_at = now
        # #269: this fact closed the item's latest command (the ordering guard
        # above is what makes that true), so its validators are the pair the
        # next command may replay. Always an overwrite, NULLs included — the
        # pair must describe the latest 200.
        record_validators(watched_item, etag=row.etag, last_modified=row.last_modified, now=now)
        stamp_full_fetch(watched_item, now=now)
        await _record_check_success(session, watched_item, result, now=now, url=row.url)

    return {
        "applied": True,
        "changed": result.changed,
        "baseline_established": result.baseline_established,
    }


@bp.task(name="apply_fetch_failure", queue="default", retry=_APPLY_RETRY)
async def apply_fetch_failure(command_id: str) -> dict:
    """Surface a terminal ``fetch_failed`` on the WatchedItem (#241 apply path).

    Mirrors the local fetch-failure path exactly: ERROR health, fresh
    ``last_checked_at`` (stops schedule_tick re-enqueueing every minute),
    ``CHECK_FETCH_FAILED`` audit carrying the reason token, and one
    ``WATCH_ERROR`` on the OK→ERROR transition.
    """
    async with get_session_factory()() as session:
        row = await session.get(FetchCommand, command_id)
        if row is None:
            return {"skipped": True, "reason": "unknown_command"}
        if row.applied_at is not None:
            return {"skipped": True, "reason": "already_applied"}
        watched_item = await session.get(WatchedItem, row.watched_item_id)
        if watched_item is None:
            return {"skipped": True, "reason": "watched_item_gone"}

        now = datetime.now(UTC)
        row.applied_at = now
        if row.failure_reason == INVALID_REQUEST_OPTIONS_REASON:
            # #269's one loop hazard. This refusal happens BEFORE any request
            # goes out, so an unsendable stored validator would be re-snapshotted
            # and refused on every cycle, forever — each one an ERROR health
            # transition and a WATCH_ERROR. Forgetting the pair makes the next
            # command unconditional, which is self-healing; every other reason
            # says nothing about our validators and leaves them alone.
            logger.warning(
                "command refused for its request options — clearing stored validators",
                extra={
                    "command_id": command_id,
                    "watched_item_id": str(watched_item.id),
                    "request_etag": row.request_etag,
                    "request_last_modified": row.request_last_modified,
                    "detail": row.failure_detail,
                },
            )
            clear_validators(watched_item)
        audit_kwargs: dict = {"reason": row.failure_reason}
        error_metadata: dict = {"reason": row.failure_reason}
        if row.status_code is not None:
            audit_kwargs["status_code"] = row.status_code
            error_metadata["status_code"] = row.status_code
        await _record_check_failure(
            session,
            watched_item,
            now=now,
            url=row.url,
            audit_event=EventType.CHECK_FETCH_FAILED,
            audit_kwargs=audit_kwargs,
            error_metadata=error_metadata,
        )
    return {"applied": True, "reason": row.failure_reason}


@bp.task(name="apply_fetch_not_modified", queue="default", retry=_APPLY_RETRY)
async def apply_fetch_not_modified(command_id: str) -> dict:
    """Close a 304 as a successful check that found no change (#249 part 1).

    Deferred by the content.blobs consumer for the one ``fetch_failed`` reason
    that is not a failure. The origin was asked a conditional question and
    answered "your bytes are current" — the most useful answer it can give. Sent
    down ``apply_fetch_failure`` instead, it would set ERROR health, write
    ``CHECK_FETCH_FAILED``, and fire one ``WATCH_ERROR`` on the OK→ERROR
    transition, on *every* successful no-change check.

    Two decisions this path records, both left open by the issue:

    * **The row's status is its own member, ``NOT_MODIFIED``**, not
      ``SUCCEEDED``-with-no-blob. ``SUCCEEDED`` means "a blob went through the
      pipeline"; overloading it makes a 304 indistinguishable in every
      status-keyed query from a real apply, and dilutes exactly the signal an
      operator reads. It costs no migration — ``status`` is a plain
      ``String(20)`` with no CHECK constraint or PG enum behind it — and
      ``OPEN_STATUSES`` is a positive enumeration, so the new member is closed to
      the scheduling gate and invisible to the reaper without either being
      touched.
    * **Nothing is written for the fingerprint.** There is no item-level
      fingerprint to reuse — Watcher's extracted-text identity lives on
      ``ChangeRevision`` rows, and ``fetch_commands.content_fingerprint`` is
      Replicator's *raw-bytes* identity for the occasion that produced bytes.
      Copying either forward would assert a fact nobody published. The item keeps
      the content it already has, which is the whole point of the exchange.

    The revision-producing half is skipped entirely: no bytes, no extraction, no
    ``ChangeRevision``, no ``PendingArchiverSync``, no ``content.revisions``
    frame. So the check is recorded through ``_record_check_success`` with an
    empty result — unchanged, no baseline — which yields ``CHECK_NO_CHANGE``,
    OK health, a fresh ``last_checked_at``, and ``WATCH_RECOVERED`` if the item
    was in ERROR. ``last_observed_at`` is stamped (#264 semantics: the content
    *was* verified current, which is more than "we tried").

    Guarded on ``applied_at`` only, mirroring ``apply_fetch_failure``: there is
    no supersession guard because there is no content to apply out of order —
    the A→B→A fingerprint flap ``apply_fetch_blob`` protects against cannot
    arise from a fact that carries no fingerprint.
    """
    async with get_session_factory()() as session:
        row = await session.get(FetchCommand, command_id)
        if row is None:
            return {"skipped": True, "reason": "unknown_command"}
        if row.applied_at is not None:
            return {"skipped": True, "reason": "already_applied"}
        watched_item = await session.get(WatchedItem, row.watched_item_id)
        if watched_item is None:
            return {"skipped": True, "reason": "watched_item_gone"}

        now = datetime.now(UTC)
        row.applied_at = now
        await _record_check_success(
            session,
            watched_item,
            WatchedItemResult(),
            now=now,
            url=row.url,
            # Keeps a 304 distinguishable from an unchanged extraction in the
            # audit trail — same outcome for the item, different evidence.
            audit_extra={"source": "not_modified"},
        )
    return {"applied": True, "not_modified": True}


@bp.periodic(cron="*/5 * * * *", periodic_id="reap_fetch_commands")
@bp.task(name="reap_fetch_commands", queue="default")
async def reap_fetch_commands(
    *, session=None, bus_client=None, batch_size: int = 100, **periodic_kwargs
) -> dict:
    """Close in-flight commands nothing else will ever close (MUST-6's backstop).

    ``fetch_failed`` covers the taxonomy's terminal rows; what remains silent is
    a stall (PEL parking, long retry) and an undecodable frame. A command
    ``in_flight`` whose **latest signal** (``coalesce(fact_at, published_at)`` —
    CR-2: a fact whose apply job died must not shield the row forever) is older
    than ``WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS`` (default 1800 — deliberately
    generous; Replicator's reclaim cadence is an operator knob on another host,
    and pinning 60s would re-issue under live retries) is handled by what it
    still has: a row holding a blob fact gets its **apply re-deferred** (the
    bytes exist — refetching would waste an origin request), anything else is
    expired and **re-issued** under a fresh ``command_id``, same intent. At
    ``WATCHER_FETCH_MAX_REISSUES`` (default 3) the intent stops: ERROR health +
    WATCH_ERROR, and the item re-enters normal scheduling — the gate lifts, so
    recovery is automatic when the origin or Replicator heals.
    """
    timeout = fetch_command_timeout_seconds()
    max_reissues = fetch_max_reissues()
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=timeout)

    owns_session = session is None
    ctx = get_session_factory()() if owns_session else None
    db = await ctx.__aenter__() if owns_session else session
    reissued, capped, reapplied = 0, 0, 0
    try:
        last_signal = func.coalesce(FetchCommand.fact_at, FetchCommand.published_at)
        rows = list(
            (
                await db.execute(
                    select(FetchCommand)
                    .where(
                        FetchCommand.status == FetchCommandStatus.IN_FLIGHT,
                        last_signal < cutoff,
                    )
                    .order_by(last_signal)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            watched_item = await db.get(WatchedItem, row.watched_item_id)
            if watched_item is None:
                row.status = FetchCommandStatus.EXPIRED
                await db.commit()
                continue
            if row.fact_at is not None and row.blob_uri:
                # The fact arrived but the apply never completed (job lost or
                # retries exhausted). Resurrect the apply — the status guard
                # makes a duplicate defer harmless. Touch fact_at so this row
                # waits a full window before the next resurrection attempt.
                row.fact_at = now
                await db.commit()
                await _defer_reapply(row.command_id)
                reapplied += 1
                continue
            if row.reissue_count >= max_reissues:
                row.status = FetchCommandStatus.FAILED
                row.failure_reason = "fetch_timeout"
                row.applied_at = now
                await _record_check_failure(
                    db,
                    watched_item,
                    now=now,
                    url=row.url,
                    audit_event=EventType.CHECK_FETCH_FAILED,
                    audit_kwargs={"reason": "fetch_timeout", "reissues": row.reissue_count},
                    error_metadata={"reason": "fetch_timeout"},
                )
                capped += 1
                continue
            row.status = FetchCommandStatus.EXPIRED
            await _reissue(db, watched_item, row, bus_client)
            reissued += 1
        if reissued or capped or reapplied:
            logger.info(
                "reaped stalled fetch commands",
                extra={"reissued": reissued, "capped": capped, "reapplied": reapplied},
            )
        return {"reissued": reissued, "capped": capped, "reapplied": reapplied}
    finally:
        if owns_session:
            await ctx.__aexit__(None, None, None)


async def _defer_reapply(command_id: str) -> None:
    """Best-effort re-defer of a lost apply job (CR-2); the next reaper pass
    retries if the defer itself fails."""
    try:
        await apply_fetch_blob.configure().defer_async(command_id=command_id)
    except Exception:
        logger.warning(
            "could not re-defer apply for a stalled command",
            extra={"command_id": command_id},
            exc_info=True,
        )
