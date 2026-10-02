"""content.process worker tasks (#325): shadow issue, sweep, apply, reaper.

The processing leg of a fetch occasion, in **shadow mode**
(``WATCHER_EXTRACT_MODE=shadow``): local extraction still decides every change,
and each applied blob is also sent to the processor so the comparator can judge
its answer against local's. In shadow the leg is a **side lineage** — nothing
here touches a fetch row, an item's health, or the fetch re-issue lineage, so a
processor outage costs comparator coverage and nothing else.

* ``issue_shadow_process_command`` — called by ``apply_fetch_blob`` once local
  extraction has decided (success or failure): persist the spec[0] command with
  local's answer, commit, publish. Never raises into the apply.
* ``publish_pending_process_commands`` — the second half of persist-before-
  publish, every minute, ``publish_pending_fetch_commands``' shape.
* ``apply_process_fact`` — deferred by the ``content.derived`` consumer once a
  terminal fact has settled a row: an empty outcome with a spec left chains
  the next spec (D3); anything else ends the lineage and is judged.
* ``reap_process_commands`` — the backstop for silence, under #325's downtime
  rule: **no re-issue while the processor is not consuming** (a command in the
  processor's group is not lost, and a re-issue only adds a duplicate to a
  stream that is never trimmed), a hard limit for the command that will never
  get a fact (CannObserv/processor#17), and a re-defer for a lost apply.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.bus import BUS_REDIS_URL_ENV, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.fetch_commands import fetch_max_reissues
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.fetch_command import FetchCommand
from src.core.models.process_command import (
    SETTLED_PROCESS_STATUSES,
    LocalOutcome,
    ProcessCommand,
    ProcessCommandStatus,
    ShadowVerdict,
)
from src.core.models.watched_item import WatchedItem
from src.core.process_commands import (
    ExtractMode,
    LocalExtraction,
    UnsendableProcessCommand,
    chain_process_command,
    create_process_command,
    extract_mode,
    judge_shadow,
    process_command_hard_limit_seconds,
    process_command_timeout_seconds,
    publish_process_command,
    reissue_process_command,
    select_pending_process_publish,
)
from src.core.utils import format_utc_iso
from src.workers import bp
from src.workers.pipeline import WatchedItemResult
from src.workers.retry import APPLY_RETRY

logger = get_logger(__name__)

PROCESSING_TIMEOUT = "processing_timeout"


def local_extraction_of(result: WatchedItemResult) -> LocalExtraction:
    """Local extraction's answer, as the pipeline reported it."""
    if result.baseline_established:
        outcome = LocalOutcome.BASELINE
    elif result.changed:
        outcome = LocalOutcome.CHANGED
    else:
        outcome = LocalOutcome.UNCHANGED
    return LocalExtraction(
        outcome=outcome,
        fingerprint=result.content_fingerprint,
        spec_fingerprint=result.spec_fingerprint,
    )


async def _persist_and_publish(session: AsyncSession, row: ProcessCommand, client) -> None:
    """Commit the row, then XADD it; a failed publish leaves it for the sweep."""
    await session.commit()
    publish_client = client if client is not None else get_shared_bus_client()
    if publish_client is None:
        logger.error(
            "process command cannot publish: %s is not set",
            BUS_REDIS_URL_ENV,
            extra={"command_id": row.command_id},
        )
        return
    try:
        await publish_process_command(publish_client, row)
        await session.commit()
    except Exception:
        logger.warning(
            "process command publish failed; the sweep will retry it",
            extra={"command_id": row.command_id},
            exc_info=True,
        )


async def issue_shadow_process_command(
    session: AsyncSession,
    fetch_row: FetchCommand,
    watched_item: WatchedItem,
    local: LocalExtraction,
    *,
    bus_client=None,
) -> str | None:
    """Send an applied blob to the processor beside local's answer (shadow mode only).

    Called after the apply has committed its own outcome, so a failure here
    loses one comparison and nothing else: every exception is logged and
    swallowed, and the session rolled back to the apply's committed state.
    Returns the new ``command_id``, or ``None`` when nothing was issued.
    """
    if extract_mode() is not ExtractMode.SHADOW:
        return None
    if not watched_item.source_specs:
        # Nothing to send, and local already reports the item ERROR every cycle
        # (#260): a WARNING here too would only repeat it (CR 7).
        logger.debug(
            "watched item has no source_specs — not shadowing the occasion",
            extra={"fetch_command_id": fetch_row.command_id},
        )
        return None
    if local.outcome is not LocalOutcome.EXTRACTION_FAILED and local.fingerprint is None:
        # Reading a missing fingerprint as "local failed" would manufacture a
        # mismatch; with nothing to compare against, there is nothing to send.
        logger.warning(
            "local result carries no fingerprint — not shadowing the occasion",
            extra={"fetch_command_id": fetch_row.command_id},
        )
        return None
    try:
        row = await create_process_command(
            session, fetch_row, watched_item, now=datetime.now(UTC), local=local
        )
        await _persist_and_publish(session, row, bus_client)
        return row.command_id
    except UnsendableProcessCommand as exc:
        logger.warning(
            "occasion is unsendable to the processor — not shadowing it",
            extra={"fetch_command_id": fetch_row.command_id, "error": str(exc)},
        )
    except Exception:
        logger.warning(
            "shadow process command failed — local outcome stands",
            extra={"fetch_command_id": fetch_row.command_id},
            exc_info=True,
        )
    await session.rollback()
    return None


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


def _record_verdict(
    session: AsyncSession, row: ProcessCommand, verdict: ShadowVerdict, detail: str | None
) -> None:
    """Write the comparator's verdict on the row that ended the lineage, and say it.

    A mismatch is the switch's gate (#326), so it is audited as well as logged:
    the audit trail is what an operator counts across the shadow window.
    """
    row.shadow_verdict = verdict
    row.shadow_detail = detail
    fields = {
        "command_id": row.command_id,
        "intent_id": row.intent_id,
        "fetch_command_id": row.fetch_command_id,
        "watched_item_id": str(row.watched_item_id),
        "spec_index": row.spec_index,
        "detail": detail,
    }
    if verdict is ShadowVerdict.MISMATCH:
        logger.warning("shadow extraction mismatch", extra=fields)
        audit(
            session,
            EventType.CHECK_SHADOW_MISMATCH,
            watched_item_id=str(row.watched_item_id),
            command_id=row.command_id,
            fetch_command_id=row.fetch_command_id,
            spec_index=row.spec_index,
            local_outcome=row.local_outcome,
            local_fingerprint=row.local_fingerprint,
            output_digest=row.output_digest,
            processor_version=row.processor_version,
            failure_reason=row.failure_reason,
            detail=detail,
        )
    elif verdict is ShadowVerdict.UNCOMPARED:
        logger.warning("shadow lineage ended uncompared", extra=fields)
    else:
        logger.info("shadow extraction match", extra=fields)


@bp.task(name="apply_process_fact", queue="default", retry=APPLY_RETRY)
async def apply_process_fact(command_id: str, bus_client=None) -> dict:
    """Act on a settled process command: chain the next spec, or judge the lineage.

    Deferred by the ``content.derived`` consumer after the first terminal fact
    settled the row; guarded on ``applied_at`` so a re-defer is a no-op. Runs in
    any mode — a fact for a command issued under ``shadow`` is still judged
    after the mode is turned back to ``local``.

    An empty outcome with a spec left is the fallback loop's next turn (D3):
    a fresh command for spec[i+1] under the same intent, from the item's
    *current* specs — the residual is a spec edit landing mid-chain, which the
    comparator reports rather than hides. Empty on the last spec ends the
    lineage like any other answer.
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

        if row.status == ProcessCommandStatus.COMPLETED and row.empty:
            watched_item = await session.get(WatchedItem, row.watched_item_id)
            specs = (watched_item.source_specs or []) if watched_item is not None else []
            next_index = row.spec_index + 1
            if next_index < len(specs):
                try:
                    nxt = await chain_process_command(session, row, specs[next_index], now=now)
                except UnsendableProcessCommand as exc:
                    logger.warning(
                        "next spec is unsendable — judging the lineage as it stands",
                        extra={"command_id": command_id, "error": str(exc)},
                    )
                else:
                    await _persist_and_publish(session, nxt, bus_client)
                    return {"chained": nxt.command_id, "spec_index": next_index}

        verdict, detail = judge_shadow(row)
        _record_verdict(session, row, verdict, detail)
        await session.commit()
    return {"verdict": verdict.value, "detail": detail}


def _give_up(session: AsyncSession, row: ProcessCommand, *, now: datetime, why: str) -> None:
    """End a lineage the processor never answered: expired, uncompared."""
    row.status = ProcessCommandStatus.EXPIRED
    row.applied_at = now
    _record_verdict(session, row, ShadowVerdict.UNCOMPARED, f"{PROCESSING_TIMEOUT}: {why}")


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
      the lineage ends uncompared;
    * not read past → hold it. It waits in the processor's group: either the
      processor is down, or it is back and draining the backlog in stream order
      — and "any recent fact" would read that recovery as license to re-issue
      everything still queued (CR 1). One warning per pass is the
      service-level signal;
    * either way, past ``WATCHER_PROCESS_COMMAND_HARD_LIMIT_SECONDS`` since it
      was issued, the lineage ends uncompared — the command whose failure fact
      was refused gets no fact at all, and in a quiet period nothing else
      arrives to say the processor is up.

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
        for row in stale:
            # CR 2: the consumer may be settling this row right now. Re-read it
            # locked — per row, since each commit below ends the transaction —
            # and act only if it is still in flight.
            await db.refresh(row, with_for_update=True)
            if row.status != ProcessCommandStatus.IN_FLIGHT:
                continue
            if row.issued_at < hard_cutoff:
                _give_up(db, row, now=now, why="hard limit")
                await db.commit()
                hard_limited += 1
                continue
            if read_past is None or row.published_at >= read_past:
                held += 1
                oldest_held = oldest_held or row
                continue
            if row.reissue_count >= max_reissues:
                _give_up(db, row, now=now, why="re-issue cap")
                await db.commit()
                capped += 1
                continue
            row.status = ProcessCommandStatus.EXPIRED
            nxt = await reissue_process_command(db, row, now=now)
            await _persist_and_publish(db, nxt, bus_client)
            reissued += 1

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
                "processor not consuming — holding in-flight process commands",
                extra={
                    "held": held,
                    "oldest_issued_at": format_utc_iso(oldest_held.issued_at),
                    "latest_fact_at": format_utc_iso(latest_fact) if latest_fact else None,
                },
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
