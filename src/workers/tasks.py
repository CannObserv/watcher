"""Procrastinate task wrappers: ``check_watched_item`` and ``schedule_tick``.

#185 Phase A step 6. Health status, last_checked_at, and last_changed_at live on
WatchedItem (not per Watch); ``schedule_tick`` uses WatchedItem's
last_checked_at to determine whether a cycle is due.

Since the Phase-4 cutover (#241) ``check_watched_item`` issues a
``content.fetch`` command rather than fetching: Replicator performs the request
and the resulting fact drives the apply path, which is what actually stamps
health and ``last_checked_at`` (``src/workers/fetch_commands.py``).
"""

from datetime import UTC, datetime

import procrastinate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.bus import BUS_REDIS_URL_ENV, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.fetch_commands import (
    create_fetch_command,
    has_open_command,
    publish_fetch_command,
)
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.watched_item import WatchedItem
from src.core.scheduling.cadence import compute_next_check
from src.core.scheduling.resolution import resolved_schedule_config
from src.workers import bp

logger = get_logger(__name__)


async def issue_fetch_command(
    session: AsyncSession,
    watched_item,
    bus_client,
    *,
    force_full_fetch: bool = False,
    check_command_id: str | None = None,
) -> dict:
    """Issue one ``content.fetch`` command for the item (#241, MUST-1/MUST-2).

    Persist-commit-publish, in that order: a crash after the commit leaves a
    ``pending_publish`` row the every-minute sweep republishes under the same
    ``command_id`` (idempotent by Replicator's dedupe). No politeness sleep —
    per-host pacing is Replicator's since #245 — and no ``last_checked_at``
    stamp: the check has not happened yet.

    The open-command gate keeps ``schedule_tick`` from re-issuing every tick
    against a silently failed command (the contract's most expensive
    issuer-side hazard).

    Shared with the ``info.watch-check`` consumer (#371), which passes the
    command's id as ``check_command_id``. Anything the caller staged on
    ``session`` commits with the row.
    """
    if await has_open_command(session, watched_item.id):
        logger.info(
            "fetch command already open — not re-issuing",
            extra={"watched_item_id": str(watched_item.id)},
        )
        return {"skipped": True, "reason": "command_in_flight"}

    now = datetime.now(UTC)
    row = await create_fetch_command(
        session,
        watched_item,
        now=now,
        force_full_fetch=force_full_fetch,
        check_command_id=check_command_id,
    )
    # Operator breadcrumb on the item's Recent Activity: the check is now a
    # command in flight; the apply-side events pick the story up (#241 CR-8).
    audit(
        session,
        EventType.CHECK_COMMAND_ISSUED,
        watched_item_id=str(watched_item.id),
        command_id=row.command_id,
    )
    await session.commit()  # MUST-2: the correlation row is durable before any XADD

    # Shared, lifespan-owned client (#241 CR-4) — never closed here.
    client = bus_client if bus_client is not None else get_shared_bus_client()
    if client is None:
        logger.error(
            "cannot publish fetch command: %s is not set — row stays pending for the sweep",
            BUS_REDIS_URL_ENV,
        )
        return {"issued": row.command_id, "published": False}
    try:
        await publish_fetch_command(client, row, now=now)
        await session.commit()
    except Exception:
        logger.warning(
            "fetch command publish failed; the sweep will retry it",
            extra={"command_id": row.command_id},
            exc_info=True,
        )
        return {"issued": row.command_id, "published": False}
    return {"issued": row.command_id, "published": True}


# ---------------------------------------------------------------------------
# check_watched_item — periodic per-WatchedItem fetch-command issue.
# ---------------------------------------------------------------------------


@bp.task(
    name="check_watched_item",
    queue="default",
    # No origin request happens here any more (#241 step 5), so the httpx
    # exceptions are unreachable; what is left retryable is the DB read and the
    # persist-before-publish write. Broker failures are deliberately absent —
    # issue_fetch_command swallows them and the sweep republishes.
    retry=procrastinate.RetryStrategy(
        max_attempts=3,
        exponential_wait=5,
        retry_exceptions={ConnectionError, TimeoutError},
    ),
)
async def check_watched_item(
    watched_item_id: str, bus_client=None, force_full_fetch: bool = False
) -> dict:
    """Issue a ``content.fetch`` command for the WatchedItem's URL.

    Since the Phase-4 cutover (#241) Watcher does not fetch: it publishes a
    command and returns. The fact that comes back on ``content.blobs`` drives
    the pipeline, health, ``last_checked_at`` and the check audits — all in the
    apply path (``src/workers/fetch_commands.py``). This task's remaining job is
    the guards plus the issue itself.

    ``bus_client`` is a test seam; production uses the shared lifespan client.
    ``force_full_fetch`` suppresses conditional GET for this occasion (#269): the
    check-now route sets it, so an operator who suspects a stale validator can
    force a real re-read without touching the database.
    """
    async with get_session_factory()() as session:
        watched_item = await session.get(WatchedItem, ULID.from_str(watched_item_id))
        if watched_item is None:
            logger.warning("watched_item not found", extra={"watched_item_id": watched_item_id})
            return {"skipped": True}

        if (
            not watched_item.is_active
            or watched_item.archived_at is not None
            or watched_item.domain_suspended
        ):
            logger.info(
                "watched_item inactive, archived, or domain-suspended",
                extra={"watched_item_id": watched_item_id},
            )
            return {"skipped": True}

        if not watched_item.effective_url:
            logger.warning(
                "watched_item has no effective_url — skipping until create populates it",
                extra={"watched_item_id": watched_item_id},
            )
            return {"skipped": True, "reason": "no_effective_url"}

        return await issue_fetch_command(
            session, watched_item, bus_client, force_full_fetch=force_full_fetch
        )


# ---------------------------------------------------------------------------
# schedule_tick — enqueue check_watched_item per due WatchedItem.
# ---------------------------------------------------------------------------


@bp.periodic(cron="* * * * *")
@bp.task(name="schedule_tick", queue="default")
async def schedule_tick(timestamp: int) -> None:
    """Enqueue ``check_watched_item`` jobs for every WatchedItem due now.

    A WatchedItem is "due" when ``last_checked_at`` is NULL (never checked) or
    when its resolved schedule — announced, else the system default — says the
    next check is overdue.

    Since F2 (#375) the tick evaluates no temporal profiles and runs no
    post-actions: Archiver runs the profile boundary and announces the outcome
    (CannObserv/archiver#365). The ``deactivate``/``archive`` post-actions wrote
    ``is_active``/``archived_at`` here without the registry guard, and
    ``reduce_frequency`` wrote ``throttle_floor_interval``; the tick writes
    nothing now.
    """
    now = datetime.now(UTC)

    async with get_session_factory()() as session:
        # Load active, non-archived, non-domain-suspended WatchedItems — the
        # single monitored entity (#191). domain_suspended cascades from domain
        # deactivation and gates scheduling directly.
        wi_stmt = select(WatchedItem).where(
            WatchedItem.is_active.is_(True),
            WatchedItem.archived_at.is_(None),
            WatchedItem.domain_suspended.is_(False),
        )
        watched_items = list((await session.execute(wi_stmt)).scalars().all())

        deferred = 0
        for wi in watched_items:
            # Due iff never checked, or the resolved schedule is overdue.
            if wi.last_checked_at is None:
                due_now = True
            else:
                next_due = compute_next_check(
                    schedule_config=resolved_schedule_config(wi),
                    last_checked_at=wi.last_checked_at,
                    now=now,
                )
                due_now = next_due <= now

            if due_now:
                logger.info(
                    "scheduling watched_item",
                    extra={"watched_item_id": str(wi.id)},
                )
                await check_watched_item.configure().defer_async(watched_item_id=str(wi.id))
                deferred += 1

    if deferred:
        logger.info("schedule_tick deferred checks", extra={"count": deferred})
