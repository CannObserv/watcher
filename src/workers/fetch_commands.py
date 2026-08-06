"""Phase-4 fetch-command worker tasks (#241): sweep, apply, failure, reaper.

* ``publish_pending_fetch_commands`` — the second half of persist-before-publish
  (issuer contract MUST-2): a crash or broker outage between a row's commit and
  its XADD leaves ``pending_publish``, republished **under the same
  ``command_id``** (idempotent by Replicator's dedupe). Every minute; no-op
  query when idle.
* ``apply_fetch_blob`` / ``apply_fetch_failure`` — deferred by the
  content.blobs consumer (``src/workers/fetch_facts.py``) after it upserts a
  fact; they restore the exact bookkeeping the local fetch path performs
  (health, ``last_checked_at``, audits, WATCH_ERROR/RECOVERED).
* ``reap_fetch_commands`` — MUST-6's backstop for the still-silent outcomes
  (stalls, undecodable frames): expire + re-issue with intent lineage, cap with
  an ERROR surface.
"""

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from urllib.request import url2pathname

from sqlalchemy import func, select

from src.core.database import get_session_factory
from src.core.fetch_commands import (
    create_fetch_command,
    publish_fetch_command,
    select_pending_publish,
)
from src.core.fetch_policy import BUS_REDIS_URL_ENV, bus_client_from_env
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.models.watched_item import CONTENT_MEDIA_TYPE_MAX_LEN, WatchedItem
from src.core.registry import ServiceRegistry, get_registry
from src.workers import bp
from src.workers.pipeline import ExtractionError, process_watched_item
from src.workers.tasks import _record_check_failure, _record_check_success

logger = get_logger(__name__)


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

        client = bus_client if bus_client is not None else bus_client_from_env()
        if client is None:
            logger.error(
                "cannot republish %d pending fetch command(s): %s is not set",
                len(rows),
                BUS_REDIS_URL_ENV,
            )
            return {"published": 0, "skipped": f"{BUS_REDIS_URL_ENV} not set"}
        owns_client = bus_client is None

        published = 0
        try:
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
        finally:
            if owns_client:
                await client.aclose()
        if published:
            logger.info("republished pending fetch commands", extra={"published": published})
        return {"published": published}
    finally:
        if owns_session:
            await ctx.__aexit__(None, None, None)


async def _reissue(session, watched_item, prior, client) -> str:
    """Re-issue an intent under a fresh ``command_id`` (MUST-6: a timeout or a
    lost blob is grounds to re-issue, never to conclude failure).

    Same ``intent_id``, ``reissue_count + 1``. Persist-commit-publish, like the
    original issue; a failed publish leaves ``pending_publish`` for the sweep.
    Returns the new ``command_id``.
    """
    now = datetime.now(UTC)
    row = await create_fetch_command(
        session,
        watched_item,
        now=now,
        intent_id=prior.intent_id,
        reissue_count=prior.reissue_count + 1,
    )
    await session.commit()
    publish_client = client if client is not None else bus_client_from_env()
    if publish_client is None:
        logger.error("re-issued fetch command cannot publish: %s is not set", BUS_REDIS_URL_ENV)
        return row.command_id
    owns = client is None
    try:
        await publish_fetch_command(publish_client, row, now=now)
        await session.commit()
    except Exception:
        logger.warning(
            "re-issue publish failed; the sweep will retry it",
            extra={"command_id": row.command_id},
            exc_info=True,
        )
    finally:
        if owns:
            await publish_client.aclose()
    return row.command_id


def _blob_path(blob_uri: str) -> str:
    """Filesystem path for a ``file://`` blob URI (host-local by contract, MUST-7)."""
    parsed = urlparse(blob_uri)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported blob_uri scheme: {blob_uri!r}")
    return url2pathname(parsed.path)


@bp.task(name="apply_fetch_blob", queue="default")
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
    not last read, so waiting cannot help.
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
            with open(_blob_path(row.blob_uri), "rb") as fh:
                raw_content = fh.read()
        except (OSError, ValueError) as exc:
            logger.warning(
                "blob unreadable — re-issuing the intent",
                extra={"command_id": command_id, "blob_uri": row.blob_uri, "error": str(exc)},
            )
            row.status = FetchCommandStatus.EXPIRED
            new_id = await _reissue(session, watched_item, row, bus_client)
            return {"reissued": new_id}

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
            )
        except ExtractionError as exc:
            logger.warning(
                "extraction failed on applied blob",
                extra={"command_id": command_id, "error": str(exc)},
            )
            row.status = FetchCommandStatus.FAILED
            row.applied_at = now
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
        await _record_check_success(session, watched_item, result, now=now, url=row.url)

    return {
        "applied": True,
        "changed": result.changed,
        "baseline_established": result.baseline_established,
    }


@bp.task(name="apply_fetch_failure", queue="default")
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


@bp.periodic(cron="*/5 * * * *", periodic_id="reap_fetch_commands")
@bp.task(name="reap_fetch_commands", queue="default")
async def reap_fetch_commands(
    *, session=None, bus_client=None, batch_size: int = 100, **periodic_kwargs
) -> dict:
    """Close in-flight commands nothing else will ever close (MUST-6's backstop).

    ``fetch_failed`` covers the taxonomy's terminal rows; what remains silent is
    a stall (PEL parking, long retry) and an undecodable frame. A command
    ``in_flight`` with no fact past ``WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS``
    (default 1800 — deliberately generous; Replicator's reclaim cadence is an
    operator knob on another host, and pinning 60s would re-issue under live
    retries) is expired and **re-issued** under a fresh ``command_id``, same
    intent. At ``WATCHER_FETCH_MAX_REISSUES`` (default 3) the intent stops:
    ERROR health + WATCH_ERROR, and the item re-enters normal scheduling — the
    gate lifts, so recovery is automatic when the origin or Replicator heals.
    """
    timeout = float(os.environ.get("WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS", "1800"))
    max_reissues = int(os.environ.get("WATCHER_FETCH_MAX_REISSUES", "3"))
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=timeout)

    owns_session = session is None
    ctx = get_session_factory()() if owns_session else None
    db = await ctx.__aenter__() if owns_session else session
    reissued, capped = 0, 0
    try:
        rows = list(
            (
                await db.execute(
                    select(FetchCommand)
                    .where(
                        FetchCommand.status == FetchCommandStatus.IN_FLIGHT,
                        FetchCommand.fact_at.is_(None),
                        FetchCommand.published_at < cutoff,
                    )
                    .order_by(FetchCommand.published_at)
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
        if reissued or capped:
            logger.info(
                "reaped stalled fetch commands",
                extra={"reissued": reissued, "capped": capped},
            )
        return {"reissued": reissued, "capped": capped}
    finally:
        if owns_session:
            await ctx.__aexit__(None, None, None)
