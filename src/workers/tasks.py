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
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watched_item import WatchedItem
from src.core.scheduling.cadence import compute_next_check, evaluate_post_actions, parse_interval
from src.core.scheduling.resolution import resolved_schedule_config
from src.workers import bp
from src.workers.watch_status import defer_status_republish

logger = get_logger(__name__)


async def _issue_fetch_command(
    session: AsyncSession, watched_item, bus_client, *, force_full_fetch: bool = False
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
    """
    if await has_open_command(session, watched_item.id):
        logger.info(
            "fetch command already open — not re-issuing",
            extra={"watched_item_id": str(watched_item.id)},
        )
        return {"skipped": True, "reason": "command_in_flight"}

    now = datetime.now(UTC)
    row = await create_fetch_command(
        session, watched_item, now=now, force_full_fetch=force_full_fetch
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
    # _issue_fetch_command swallows them and the sweep republishes.
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

        return await _issue_fetch_command(
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
    when its resolved schedule (with its optional 1:1 temporal profile applied)
    says the next check is overdue.

    Post-actions on the WatchedItem's temporal profile:
    * ``deactivate`` flips the WatchedItem inactive.
    * ``archive`` flips it inactive and stamps ``archived_at``.
    * ``reduce_frequency`` slows ``default_schedule_config`` to ``1d``;
      audited as ``WATCHED_ITEM_THROTTLED``.
    """
    now = datetime.now(UTC)

    def _profile_dicts(profiles_orm: list[TemporalProfile]) -> list[dict] | None:
        if not profiles_orm:
            return None
        return [p.to_resolution_dict() for p in profiles_orm]

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

        if not watched_items:
            return

        wi_ids = [wi.id for wi in watched_items]

        # Batch-load each WatchedItem's temporal profile (#191: 1:1 on WatchedItem).
        profiles_by_wi: dict[str, list[TemporalProfile]] = {}
        tp_stmt = select(TemporalProfile).where(
            TemporalProfile.is_active.is_(True),
            TemporalProfile.watched_item_id.in_(wi_ids),
        )
        for p in (await session.execute(tp_stmt)).scalars().all():
            profiles_by_wi.setdefault(str(p.watched_item_id), []).append(p)

        deferred = 0
        status_moved = False  # any wire-visible post-action mutation (#264)
        for wi in watched_items:
            profiles_orm = profiles_by_wi.get(str(wi.id), [])

            # Apply the WatchedItem's temporal post-actions first; a
            # reduce_frequency action mutates the schedule used for the due check.
            if profiles_orm:
                actions = evaluate_post_actions(_profile_dicts(profiles_orm), today=now.date())
                for action_info in actions:
                    action = action_info["action"]
                    profile_dict = action_info["profile"]
                    orm_profile = next(
                        (p for p in profiles_orm if str(p.id) == profile_dict["id"]),
                        None,
                    )
                    if action == "deactivate":
                        wi.is_active = False
                        status_moved = True
                        logger.info(
                            "post-action: deactivate watched_item",
                            extra={"watched_item_id": str(wi.id), "profile_id": profile_dict["id"]},
                        )
                    elif action == "archive":
                        wi.is_active = False
                        wi.archived_at = now
                        status_moved = True
                        logger.info(
                            "post-action: archive watched_item",
                            extra={"watched_item_id": str(wi.id), "profile_id": profile_dict["id"]},
                        )
                    elif action == "reduce_frequency":
                        # Throttle to 1d only when the currently-effective cadence is
                        # faster than 1d. With the Domain cadence tier (#205) an item
                        # may already resolve to a slower interval (e.g. a 7d domain);
                        # pinning it to 1d would *speed it up*, the opposite of
                        # "reduce frequency". When already ≥1d this is a no-op and no
                        # floor is set, preserving inheritance.
                        #
                        # Writes the *floor*, not the item config (#254). A throttle is
                        # protective mechanism, not cadence policy: policy is the
                        # registry's since the info.registry reconcile landed, and a
                        # throttle written into default_schedule_config would be
                        # outranked by the announced tier — silently un-throttling the
                        # item on its next announcement, which is the failure this
                        # column split exists to prevent.
                        if parse_interval(
                            resolved_schedule_config(wi).get("interval")
                        ) < parse_interval("1d"):
                            wi.throttle_floor_interval = "1d"
                            status_moved = True
                            audit(
                                session,
                                EventType.WATCHED_ITEM_THROTTLED,
                                watched_item_id=str(wi.id),
                                new_interval="1d",
                            )
                            logger.info(
                                "post-action: reduce frequency on watched_item",
                                extra={
                                    "watched_item_id": str(wi.id),
                                    "profile_id": profile_dict["id"],
                                },
                            )
                    if orm_profile is not None:
                        orm_profile.is_active = False

            # Skip if a post-action just turned this WatchedItem off.
            if not wi.is_active or wi.archived_at is not None:
                continue

            # Due iff never checked, or the resolved schedule is overdue.
            if wi.last_checked_at is None:
                due_now = True
            else:
                next_due = compute_next_check(
                    schedule_config=resolved_schedule_config(wi),
                    last_checked_at=wi.last_checked_at,
                    now=now,
                    profiles=_profile_dicts(profiles_orm),
                )
                due_now = next_due <= now

            if due_now:
                logger.info(
                    "scheduling watched_item",
                    extra={"watched_item_id": str(wi.id)},
                )
                await check_watched_item.configure().defer_async(watched_item_id=str(wi.id))
                deferred += 1

        await session.commit()

    if status_moved:
        # A post-action changed applied_active or the floor (applied_interval)
        # — one republish covers the tick's whole batch, post-commit (#264).
        await defer_status_republish()

    if deferred:
        logger.info("schedule_tick deferred checks", extra={"count": deferred})
