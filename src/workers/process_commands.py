"""content.process worker tasks (#325, #326): sweep, apply, reaper.

The processing leg of a fetch occasion (#326): the fetch row waits
``PROCESSING`` and the lineage's end closes it — the derived text goes through
the history comparison and Option A, a failure is the extraction-failure path,
an unreadable input re-fetches, and a timeout fails the check. A lineage whose
check has already closed (superseded, or given up on) decides nothing.

* ``publish_pending_process_commands`` — the second half of persist-before-
  publish, every minute, ``publish_pending_fetch_commands``' shape.
* ``apply_process_fact`` — deferred by the ``content.derived`` consumer once a
  terminal fact has settled a row: an empty outcome with a spec left chains
  the next spec (D3); anything else ends the lineage.
* ``reap_process_commands`` — the backstop for silence, under #325's downtime
  rule: **no re-issue until the processor has read past a command** (one in the
  processor's group is not lost, and a re-issue only adds a duplicate to a
  stream that is never trimmed), a hard limit for the command that will never
  get a fact (CannObserv/processor#17), and a re-defer for a lost apply. A held
  decisive command is *delay*, never failure: its item stays as it was.
"""

from datetime import UTC, datetime, timedelta

from co_core.pure.extract import CANONICAL_TEXT_MEDIA_TYPE, spec_schema_version
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.bus import BUS_REDIS_URL_ENV, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.fetch_commands import fetch_max_reissues
from src.core.logging import get_logger
from src.core.models.audit_log import EventType
from src.core.models.fetch_command import (
    PROCESSING_FAILED_REASON,
    PROCESSING_TIMEOUT_REASON,
    FetchCommand,
    FetchCommandStatus,
)
from src.core.models.process_command import (
    SETTLED_PROCESS_STATUSES,
    ProcessCommand,
    ProcessCommandStatus,
)
from src.core.models.watched_item import WatchedItem
from src.core.process_commands import (
    INPUT_UNREADABLE_REASON,
    UnsendableProcessCommand,
    chain_process_command,
    process_command_hard_limit_seconds,
    process_command_timeout_seconds,
    publish_process_command,
    reissue_process_command,
    select_pending_process_publish,
)
from src.core.utils import format_utc_iso
from src.workers import bp
from src.workers.fetch_commands import (
    close_succeeded,
    fail_blob_unreadable,
    fail_extraction,
    record_check_failure,
    reissue_fetch_command,
)
from src.workers.pipeline import BlobProvenance, ExtractionOutcome, apply_extraction_outcome
from src.workers.process_issue import persist_and_publish
from src.workers.retry import APPLY_RETRY

logger = get_logger(__name__)


@bp.periodic(cron="* * * * *", periodic_id="publish_pending_process_commands")
@bp.task(name="publish_pending_process_commands", queue="default")
async def publish_pending_process_commands(
    *, session=None, bus_client=None, batch_size: int = 100, **periodic_kwargs
) -> dict:
    """Republish every ``pending_publish`` process command under its own id.

    ``publish_pending_fetch_commands``' shape: ``session`` / ``bus_client`` are
    test seams, a per-row failure stays pending for the next tick, and the
    missing-env error is raised only when there is work to publish.
    """
    owns_session = session is None
    ctx = get_session_factory()() if owns_session else None
    db = await ctx.__aenter__() if owns_session else session
    try:
        rows = await select_pending_process_publish(db, limit=batch_size)
        if not rows:
            return {"published": 0}
        client = bus_client if bus_client is not None else get_shared_bus_client()
        if client is None:
            logger.error(
                "cannot republish %d pending process command(s): %s is not set",
                len(rows),
                BUS_REDIS_URL_ENV,
            )
            return {"published": 0, "skipped": f"{BUS_REDIS_URL_ENV} not set"}
        published = 0
        for row in rows:
            try:
                await publish_process_command(client, row)
                await db.commit()
                published += 1
            except Exception:
                logger.warning(
                    "process command republish failed; will retry next tick",
                    extra={"command_id": row.command_id},
                    exc_info=True,
                )
        if published:
            logger.info("republished pending process commands", extra={"published": published})
        return {"published": published}
    finally:
        if owns_session:
            await ctx.__aexit__(None, None, None)


@bp.task(name="apply_process_fact", queue="default", retry=APPLY_RETRY)
async def apply_process_fact(command_id: str, bus_client=None) -> dict:
    """Act on a settled process command: chain the next spec, or end the lineage.

    Deferred by the ``content.derived`` consumer after the first terminal fact
    settled the row; guarded on ``applied_at`` so a re-defer is a no-op. The
    lineage's end closes the check while its fetch row waits ``PROCESSING``
    (``_decide``); once anything else has closed it — the reaper gave up, a
    newer occasion superseded it, the row is gone — a late answer is recorded
    as applied, chains nothing and decides nothing.

    An empty outcome with a spec left is the fallback loop's next turn (D3):
    a fresh command for spec[i+1] under the same intent, from the item's
    *current* specs — the residual is a spec edit landing mid-chain. Empty on
    the last spec ends the lineage like any other answer.
    """
    async with get_session_factory()() as session:
        row = await session.get(ProcessCommand, command_id)
        if row is None:
            return {"skipped": True, "reason": "unknown_command"}
        if row.applied_at is not None:
            return {"skipped": True, "reason": "already_applied"}
        if row.status not in SETTLED_PROCESS_STATUSES:
            return {"skipped": True, "reason": f"status_{row.status}"}
        now = datetime.now(UTC)
        row.applied_at = now
        # Only an open check is waiting on this answer: the blob apply leaves
        # its fetch row PROCESSING, and nothing else does.
        fetch = await session.get(FetchCommand, row.fetch_command_id)
        if fetch is None or fetch.status != FetchCommandStatus.PROCESSING:
            logger.info(
                "processor answered a check that has already closed — nothing to decide",
                extra={
                    "command_id": command_id,
                    "fetch_command_id": row.fetch_command_id,
                    "fetch_status": fetch.status if fetch is not None else None,
                },
            )
            await session.commit()
            return {"skipped": True, "reason": "check_closed"}

        if row.status == ProcessCommandStatus.COMPLETED and row.empty:
            watched_item = await session.get(WatchedItem, row.watched_item_id)
            specs = (watched_item.source_specs or []) if watched_item is not None else []
            next_index = row.spec_index + 1
            if next_index < len(specs):
                try:
                    nxt = await chain_process_command(session, row, specs[next_index], now=now)
                except UnsendableProcessCommand as exc:
                    logger.warning(
                        "next spec is unsendable — ending the lineage as it stands",
                        extra={"command_id": command_id, "error": str(exc)},
                    )
                else:
                    await persist_and_publish(session, nxt, bus_client)
                    return {"chained": nxt.command_id, "spec_index": next_index}

        return await _decide(session, row, fetch, now=now, bus_client=bus_client)


def derived_outcome(row: ProcessCommand) -> ExtractionOutcome:
    """The outcome a non-empty ``processing_complete`` fact reports (#326).

    ``output_digest`` is ``canonical_text``'s fingerprint (cannobserv#486), so
    it *is* the ``ChangeRevision.content_fingerprint``. The fallbacks cover only a
    column the consumer always writes; the wire requires every one.
    """
    return ExtractionOutcome(
        content_fingerprint=row.output_digest,
        content_size_bytes=row.output_size_bytes or 0,
        schema_version=(
            row.spec_schema_version
            if row.spec_schema_version is not None
            else spec_schema_version(row.source_spec)
        ),
        spec_fingerprint=row.spec_fingerprint,
        content_media_type=row.output_media_type or CANONICAL_TEXT_MEDIA_TYPE,
        processor_version=row.processor_version,
    )


async def _decide(
    session: AsyncSession,
    row: ProcessCommand,
    fetch: FetchCommand,
    *,
    now: datetime,
    bus_client,
) -> dict:
    """Close a check from the fact that ended its lineage (#326).

    The design's apply table, Section 2:

    * a newer occasion for the item has already closed → ``SUPERSEDED``, nothing
      written (the blob leg's ordering guard, on the derived leg);
    * derived text → the history comparison and Option A
      (``apply_extraction_outcome``), with the raw blob's provenance from the
      fetch fact; the row closes ``SUCCEEDED``;
    * ``input_unreadable`` → the bytes are gone, not judged: re-fetch in full
      (#361), capped at ``WATCHER_FETCH_MAX_REISSUES`` across both legs (#275);
    * anything else — empty on the last spec (D5, the #258 rule), any other
      terminal reason, ``extraction_error`` from a give-up (processor#17)
      included — is the extraction-failure path. ``detail`` is recorded, never
      branched on.
    """
    watched_item = await session.get(WatchedItem, row.watched_item_id)
    newest_applied = (
        await session.execute(
            select(func.max(FetchCommand.issued_at)).where(
                FetchCommand.watched_item_id == fetch.watched_item_id,
                FetchCommand.applied_at.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if newest_applied is not None and newest_applied > fetch.issued_at:
        fetch.status = FetchCommandStatus.SUPERSEDED
        await session.commit()
        return {"skipped": True, "reason": "superseded"}

    if row.status == ProcessCommandStatus.COMPLETED and not row.empty:
        result = await apply_extraction_outcome(
            session,
            watched_item,
            derived_outcome(row),
            blob=BlobProvenance(
                command_id=fetch.command_id,
                blob_uri=fetch.blob_uri,
                source_media_type=fetch.media_type,
                blob_expires_at=fetch.blob_expires_at,
                blob_fingerprint=fetch.content_fingerprint,
            ),
        )
        await close_succeeded(
            session, watched_item, fetch, result, now=now, audit_extra={"source": "processor"}
        )
        return {
            "applied": True,
            "changed": result.changed,
            "baseline_established": result.baseline_established,
            "renewal_enqueued": result.renewal_enqueued,
            "rebaselined": result.rebaselined,
        }

    if row.failure_reason == INPUT_UNREADABLE_REASON:
        # The lineage spans both legs: whichever re-issued more is the count.
        lineage = max(fetch.reissue_count, row.reissue_count)
        detail = row.failure_detail or INPUT_UNREADABLE_REASON
        if lineage >= fetch_max_reissues():
            logger.error(
                "processor still cannot read the blob at the re-issue cap — failing the check",
                extra={"command_id": row.command_id, "reissues": lineage, "detail": detail},
            )
            return await fail_blob_unreadable(
                session, watched_item, fetch, now=now, detail=detail, reissues=lineage
            )
        logger.warning(
            "processor cannot read the blob — re-fetching",
            extra={"command_id": row.command_id, "fetch_command_id": fetch.command_id},
        )
        fetch.status = FetchCommandStatus.EXPIRED
        # Forced (#361): the blob leg's stamp vouches for bytes nobody could
        # read, so a replayed pair could close the check on a 304 with none.
        new_id = await reissue_fetch_command(
            session, watched_item, fetch, bus_client, lineage_count=lineage, force_full_fetch=True
        )
        return {"reissued": new_id}

    if row.status == ProcessCommandStatus.COMPLETED:
        detail = f"empty on every source_spec ({row.spec_index + 1} tried)"
    else:
        detail = ": ".join(part for part in (row.failure_reason, row.failure_detail) if part)
    logger.warning(
        "processor derived no text — failing the check",
        extra={"command_id": row.command_id, "detail": detail},
    )
    return await fail_extraction(
        session,
        watched_item,
        fetch,
        now=now,
        error=detail,
        reason=PROCESSING_FAILED_REASON,
        detail=detail,
    )


async def _give_up(
    session: AsyncSession, row: ProcessCommand, *, now: datetime, why: str
) -> FetchCommand | None:
    """End a lineage the processor never answered: the row expires.

    While its check is still open (the fetch row ``PROCESSING``, #326) the
    check fails too: the fetch row closes ``processing_timeout`` and the item
    goes ERROR, so the one-open-command gate lifts — returned for the caller,
    which owns the commit and the failure bookkeeping.
    """
    row.status = ProcessCommandStatus.EXPIRED
    row.applied_at = now
    fetch = await session.get(FetchCommand, row.fetch_command_id)
    if fetch is not None and fetch.status == FetchCommandStatus.PROCESSING:
        fetch.status = FetchCommandStatus.FAILED
        fetch.failure_reason = PROCESSING_TIMEOUT_REASON
        fetch.failure_detail = why
        fetch.applied_at = now
        logger.error(
            "processor never answered — failing the check",
            extra={"command_id": row.command_id, "fetch_command_id": fetch.command_id, "why": why},
        )
        return fetch
    return None


async def _end_lineage(
    session: AsyncSession, row: ProcessCommand, *, now: datetime, why: str
) -> None:
    """``_give_up``, committed — with the ERROR surface when it closed a check."""
    fetch = await _give_up(session, row, now=now, why=why)
    if fetch is None:
        await session.commit()
        return
    watched_item = await session.get(WatchedItem, fetch.watched_item_id)
    # A timeout says nothing about the stored validators: the pair is the last
    # *extracted* 200's, and a 304 against it is still a true answer.
    await record_check_failure(
        session,
        watched_item,
        now=now,
        url=fetch.url,
        audit_event=EventType.CHECK_EXTRACTION_FAILED,
        audit_kwargs={"reason": PROCESSING_TIMEOUT_REASON, "detail": why},
        error_metadata={"reason": PROCESSING_TIMEOUT_REASON},
    )


@bp.periodic(cron="*/5 * * * *", periodic_id="reap_process_commands")
@bp.task(name="reap_process_commands", queue="default")
async def reap_process_commands(
    *, session=None, bus_client=None, batch_size: int = 100, **periodic_kwargs
) -> dict:
    """Close in-flight process commands nothing else will close (#325's downtime rule).

    A command is **stale** when its latest signal (``coalesce(fact_at,
    published_at)``) is older than ``WATCHER_PROCESS_COMMAND_TIMEOUT_SECONDS``.
    What happens to it depends on whether the processor has **read past it** —
    answered some command published *after* it:

    * read past → this one command is stuck: expire it and re-issue under a
      fresh ``command_id``, capped at ``WATCHER_FETCH_MAX_REISSUES``; at the cap
      the lineage ends (``_give_up``);
    * not read past → hold it. It waits in the processor's group: either the
      processor is down, or it is back and draining the backlog in stream order
      — and "any recent fact" would read that recovery as license to re-issue
      everything still queued (CR 1). One warning per pass is the
      service-level signal;
    * either way, past ``WATCHER_PROCESS_COMMAND_HARD_LIMIT_SECONDS`` since it
      was issued, the lineage ends, failing its check if still open, because
      the command whose failure fact was refused gets no fact at all, and in a
      quiet period nothing else arrives to say the processor is up. A command
      still ``pending_publish`` past it ends the same way: the bus never
      accepted it (CR 6).

    A settled row whose apply never ran (job lost, retries exhausted) gets the
    apply re-deferred, once per window: ``updated_at`` is touched to start the
    window again, never ``fact_at``, which is the processor's own record of when
    it answered.
    """
    timeout = process_command_timeout_seconds()
    hard_limit = process_command_hard_limit_seconds()
    max_reissues = fetch_max_reissues()
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=timeout)
    hard_cutoff = now - timedelta(seconds=hard_limit)

    owns_session = session is None
    ctx = get_session_factory()() if owns_session else None
    db = await ctx.__aenter__() if owns_session else session
    reissued, capped, hard_limited, held, reapplied = 0, 0, 0, 0, 0
    try:
        latest_fact = (await db.execute(select(func.max(ProcessCommand.fact_at)))).scalar()
        # The newest command the processor has answered, by publish time. The
        # stream is read in order, so anything published before it and still
        # unanswered was passed over, not queued.
        read_past = (
            await db.execute(
                select(func.max(ProcessCommand.published_at)).where(
                    ProcessCommand.fact_at.is_not(None)
                )
            )
        ).scalar()
        last_signal = func.coalesce(ProcessCommand.fact_at, ProcessCommand.published_at)
        stale = list(
            (
                await db.execute(
                    select(ProcessCommand)
                    .where(
                        ProcessCommand.status == ProcessCommandStatus.IN_FLIGHT,
                        last_signal < cutoff,
                    )
                    .order_by(last_signal)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        oldest_held = None
        # Items whose check waits on a held decisive command (#326).
        delayed: list[str] = []
        for row in stale:
            # CR 2: the consumer may be settling this row right now. Re-read it
            # locked — per row, since each commit below ends the transaction —
            # and act only if it is still in flight.
            await db.refresh(row, with_for_update=True)
            if row.status != ProcessCommandStatus.IN_FLIGHT:
                continue
            if row.issued_at < hard_cutoff:
                await _end_lineage(db, row, now=now, why="hard limit")
                hard_limited += 1
                continue
            if read_past is None or row.published_at >= read_past:
                held += 1
                oldest_held = oldest_held or row
                fetch = await db.get(FetchCommand, row.fetch_command_id)
                if fetch is not None and fetch.status == FetchCommandStatus.PROCESSING:
                    delayed.append(str(row.watched_item_id))
                continue
            if row.reissue_count >= max_reissues:
                await _end_lineage(db, row, now=now, why="re-issue cap")
                capped += 1
                continue
            row.status = ProcessCommandStatus.EXPIRED
            nxt = await reissue_process_command(db, row, now=now)
            await persist_and_publish(db, nxt, bus_client)
            reissued += 1

        # CR 6: a command the bus never accepted gets no fact either. The sweep
        # retries a refused publish forever (an ACL refusal is transient by
        # policy), so the hard limit ends it too — or a decisive check would
        # sit PROCESSING with nothing left to close it. Residual: the unlocked
        # sweep can still overwrite this EXPIRED with its in-flight write, in
        # one sweep's window, after a day of refusals.
        unpublished = list(
            (
                await db.execute(
                    select(ProcessCommand)
                    .where(
                        ProcessCommand.status == ProcessCommandStatus.PENDING_PUBLISH,
                        ProcessCommand.issued_at < hard_cutoff,
                    )
                    .order_by(ProcessCommand.issued_at)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        for row in unpublished:
            await db.refresh(row, with_for_update=True)
            if row.status != ProcessCommandStatus.PENDING_PUBLISH:
                continue
            logger.error(
                "process command never reached the bus — ending its lineage",
                extra={"command_id": row.command_id, "issued_at": format_utc_iso(row.issued_at)},
            )
            await _end_lineage(db, row, now=now, why="hard limit, never published")
            hard_limited += 1

        unapplied = list(
            (
                await db.execute(
                    select(ProcessCommand)
                    .where(
                        ProcessCommand.status.in_(SETTLED_PROCESS_STATUSES),
                        ProcessCommand.applied_at.is_(None),
                        ProcessCommand.updated_at < cutoff,
                    )
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        for row in unapplied:
            row.updated_at = now
            await db.commit()
            await _defer_reapply(row.command_id)
            reapplied += 1

        if held:
            logger.warning(
                "processor has not reached held process commands — down, or draining its backlog",
                extra={
                    "held": held,
                    "oldest_issued_at": format_utc_iso(oldest_held.issued_at),
                    "latest_fact_at": format_utc_iso(latest_fact) if latest_fact else None,
                },
            )
        if delayed:
            # Delay, never failure: the items stay as they were — no ERROR, no
            # WATCH_ERROR — until the processor answers or the hard limit ends
            # the wait. This line is the per-item signal.
            logger.warning(
                "processing delayed — checks are waiting on the processor",
                extra={"watched_item_ids": delayed},
            )
        result = {
            "reissued": reissued,
            "capped": capped,
            "hard_limited": hard_limited,
            "held": held,
            "reapplied": reapplied,
        }
        if reissued or capped or hard_limited or reapplied:
            logger.info("reaped stalled process commands", extra=result)
        return result
    finally:
        if owns_session:
            await ctx.__aexit__(None, None, None)


async def _defer_reapply(command_id: str) -> None:
    """Best-effort re-defer of a lost apply; the next window retries a failed defer."""
    try:
        await apply_process_fact.configure().defer_async(command_id=command_id)
    except Exception:
        logger.warning(
            "could not re-defer apply for a settled process command",
            extra={"command_id": command_id},
            exc_info=True,
        )
