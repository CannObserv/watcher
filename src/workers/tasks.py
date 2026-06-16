"""Procrastinate task wrappers: ``check_watched_item`` and ``schedule_tick``.

#185 Phase A step 6. Health status, last_checked_at, and last_changed_at now
live on WatchedItem (not per Watch). ``check_watched_item`` updates the parent
WatchedItem's health and timestamp; ``schedule_tick`` uses WatchedItem's
last_checked_at to determine whether a cycle is due.
"""

from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import procrastinate
from sqlalchemy import or_, select
from ulid import ULID

from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch, WatchHealthStatus
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.rate_limiter import get_rate_limiter
from src.core.registry import ServiceRegistry, get_registry
from src.core.scheduler import compute_next_check, evaluate_post_actions
from src.core.watches.resolution import resolved_schedule_config, watch_event_base_metadata
from src.workers import bp
from src.workers.notify import dispatch_event_notifications
from src.workers.pipeline import (
    _maybe_decay_backoff,
    _persist_backoff,
    process_watched_item,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# check_watched_item — periodic per-WatchedItem fetch + pipeline + bookkeeping.
# ---------------------------------------------------------------------------


@bp.task(
    name="check_watched_item",
    queue="default",
    retry=procrastinate.RetryStrategy(
        max_attempts=3,
        exponential_wait=5,
        retry_exceptions={
            ConnectionError,
            TimeoutError,
            httpx.ConnectError,
            httpx.TimeoutException,
        },
    ),
)
async def check_watched_item(watched_item_id: str, registry: ServiceRegistry | None = None) -> dict:
    """Fetch the WatchedItem's URL and run the pipeline.

    Updates ``last_checked_at`` and ``health_status`` on the WatchedItem
    (both success and fetch-failure paths). Individual Watches receive
    WATCH_ERROR / WATCH_RECOVERED events when the WatchedItem's health
    transitions; CHANGE_DETECTED is dispatched inline by the pipeline.
    """
    reg = registry if registry is not None else get_registry()
    async with get_session_factory()() as session:
        watched_item = await session.get(WatchedItem, ULID.from_str(watched_item_id))
        if watched_item is None:
            logger.warning("watched_item not found", extra={"watched_item_id": watched_item_id})
            return {"skipped": True}

        if not watched_item.is_active or watched_item.archived_at is not None:
            logger.info(
                "watched_item inactive or archived",
                extra={"watched_item_id": watched_item_id},
            )
            return {"skipped": True}

        # Load active+non-archived child Watches up-front so we know whether
        # there's any work to do and so we can dispatch events after the
        # pipeline. The pipeline reloads these itself for CHANGE_DETECTED
        # dispatch — duplicate query is cheap and keeps surfaces decoupled.
        children = (
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
        if not children:
            logger.info(
                "watched_item has no active children",
                extra={"watched_item_id": watched_item_id},
            )
            return {"skipped": True, "reason": "no_active_children"}

        url = watched_item.effective_url
        if not url:
            logger.warning(
                "watched_item has no effective_url — skipping until Watch-create populates it",
                extra={"watched_item_id": watched_item_id},
            )
            return {"skipped": True, "reason": "no_effective_url"}

        fetch_config: dict = {}

        rate_limit_domain = watched_item.domain_name or urlparse(url).hostname or url

        async with get_rate_limiter().acquire_for_domain(rate_limit_domain):
            fetch_result = await reg.get_fetcher().fetch(url, config=fetch_config)

        if fetch_result.status_code == 429:
            new_interval = get_rate_limiter().report_rate_limited_for_domain(rate_limit_domain)
            await _persist_backoff(rate_limit_domain, new_interval, session)
            await session.commit()
            raise ConnectionError(f"Rate limited by {rate_limit_domain}")

        now = datetime.now(UTC)

        if not fetch_result.is_success:
            logger.warning(
                "fetch failed",
                extra={
                    "watched_item_id": watched_item_id,
                    "status": fetch_result.status_code,
                },
            )
            audit(
                session,
                EventType.CHECK_FETCH_FAILED,
                watched_item_id=str(watched_item.id),
                status_code=fetch_result.status_code,
            )
            # Track health transition on WatchedItem; dispatch WATCH_ERROR to
            # every active child Watch if WatchedItem transitions to ERROR.
            previous_health = watched_item.health_status
            watched_item.health_status = WatchHealthStatus.ERROR
            watched_item.last_checked_at = now
            await session.commit()

            if previous_health != WatchHealthStatus.ERROR:
                for w in children:
                    error_event = WatchEvent(
                        event_type=WatchEventType.WATCH_ERROR,
                        watch_id=str(w.id),
                        watch_name=w.name,
                        watch_url=watched_item.effective_url or url,
                        occurred_at=now,
                        metadata={
                            "status_code": fetch_result.status_code,
                            **watch_event_base_metadata(w),
                        },
                    )
                    await dispatch_event_notifications(session=session, event=error_event)
                await session.commit()
            return {"error": f"HTTP {fetch_result.status_code}"}

        # Successful fetch → run the per-WatchedItem pipeline.
        result = await process_watched_item(
            session=session,
            watched_item=watched_item,
            raw_content=fetch_result.content,
        )

        # Audit the successful check so executions leave a trail (the dashboard
        # checks_today stat + WatchedItem activity read these). A snapshot event
        # marks a baseline/changed cycle (a ChangeRevision was written); otherwise
        # the content was unchanged.
        snapshot = result.baseline_established or result.changed
        audit(
            session,
            EventType.CHECK_SNAPSHOT_CREATED if snapshot else EventType.CHECK_NO_CHANGE,
            watched_item_id=str(watched_item.id),
            changed=result.changed,
            baseline=result.baseline_established,
        )

        # Track health + timestamp on WatchedItem.
        previous_health = watched_item.health_status
        watched_item.health_status = WatchHealthStatus.OK
        watched_item.last_checked_at = now
        await session.commit()

        # Domain backoff decay after successful fetch.
        _limiter = get_rate_limiter()
        _state = _limiter._domains.get(rate_limit_domain)
        if _state and _state.current_interval > _state.min_interval:
            await _maybe_decay_backoff(rate_limit_domain, _limiter, session)
            await session.commit()

        # Recovery events: dispatch WATCH_RECOVERED per child Watch when
        # WatchedItem transitions ERROR → OK.
        if previous_health == WatchHealthStatus.ERROR:
            for w in children:
                recovery_event = WatchEvent(
                    event_type=WatchEventType.WATCH_RECOVERED,
                    watch_id=str(w.id),
                    watch_name=w.name,
                    watch_url=watched_item.effective_url or url,
                    occurred_at=now,
                    metadata=watch_event_base_metadata(w),
                )
                await dispatch_event_notifications(session=session, event=recovery_event)
            await session.commit()

    return {
        "baseline_established": result.baseline_established,
        "cache_hit": result.cache_hit,
        "changed": result.changed,
        "notifications_dispatched": result.notifications_dispatched,
        "archiver_sync_enqueued": result.archiver_sync_enqueued,
    }


# ---------------------------------------------------------------------------
# schedule_tick — enqueue check_watched_item per due WatchedItem.
# ---------------------------------------------------------------------------


@bp.periodic(cron="* * * * *")
@bp.task(name="schedule_tick", queue="default")
async def schedule_tick(timestamp: int) -> None:
    """Enqueue ``check_watched_item`` jobs for every WatchedItem due now.

    A WatchedItem is "due" when ``last_checked_at`` is NULL (never checked) or
    when any child Watch's resolved schedule says the next check is overdue.
    Temporal profiles live on Watch; the tightest applicable interval wins.

    Post-actions:
    * ``deactivate`` / ``archive`` flip the individual triggering Watch off.
    * ``reduce_frequency`` mutates the parent WatchedItem's
      ``default_schedule_config`` so all siblings feel the slowdown.
      Audited as ``WATCHED_ITEM_THROTTLED``.
    """
    now = datetime.now(UTC)

    async with get_session_factory()() as session:
        # Load active+non-archived WatchedItems.
        wi_stmt = select(WatchedItem).where(
            WatchedItem.is_active.is_(True),
            WatchedItem.archived_at.is_(None),
        )
        watched_items = list((await session.execute(wi_stmt)).scalars().all())

        if not watched_items:
            return

        wi_ids = [wi.id for wi in watched_items]

        # Load all active+non-archived child Watches for those WatchedItems,
        # filtered to active domains. Join Domain via WatchedItem.domain_name.
        w_stmt = (
            select(Watch, Domain)
            .join(WatchedItem, WatchedItem.id == Watch.watched_item_id)
            .outerjoin(Domain, Domain.name == WatchedItem.domain_name)
            .where(
                Watch.watched_item_id.in_(wi_ids),
                Watch.is_active.is_(True),
                Watch.is_archived.is_(False),
                or_(Domain.id.is_(None), Domain.is_active.is_(True)),
            )
        )
        rows = (await session.execute(w_stmt)).all()
        children_by_wi: dict[ULID, list[Watch]] = {}
        for watch, _domain in rows:
            children_by_wi.setdefault(watch.watched_item_id, []).append(watch)

        # Batch-load profiles for all relevant Watches.
        all_watch_ids = [w.id for ws in children_by_wi.values() for w in ws]
        profiles_by_watch: dict[str, list[TemporalProfile]] = {}
        if all_watch_ids:
            tp_stmt = select(TemporalProfile).where(
                TemporalProfile.is_active.is_(True),
                TemporalProfile.watch_id.in_(all_watch_ids),
            )
            for p in (await session.execute(tp_stmt)).scalars().all():
                profiles_by_watch.setdefault(str(p.watch_id), []).append(p)

        deferred = 0
        for wi in watched_items:
            children = children_by_wi.get(wi.id, [])
            if not children:
                continue

            # Apply per-Watch post-actions first; a reduce_frequency action
            # mutates the WatchedItem itself, which then affects the
            # aggregated interval below.
            for watch in list(children):
                wid_str = str(watch.id)
                profiles_orm = profiles_by_watch.get(wid_str, [])
                if not profiles_orm:
                    continue
                profiles = [
                    {
                        "id": str(p.id),
                        "profile_type": p.profile_type,
                        "reference_date": p.reference_date,
                        "date_range_start": p.date_range_start,
                        "date_range_end": p.date_range_end,
                        "rules": p.rules,
                        "post_action": p.post_action,
                        "is_active": p.is_active,
                    }
                    for p in profiles_orm
                ]
                actions = evaluate_post_actions(profiles, today=now.date())
                for action_info in actions:
                    action = action_info["action"]
                    profile_dict = action_info["profile"]
                    orm_profile = next(
                        (p for p in profiles_orm if str(p.id) == profile_dict["id"]),
                        None,
                    )
                    if action == "deactivate":
                        watch.is_active = False
                        logger.info(
                            "post-action: deactivate watch",
                            extra={"watch_id": wid_str, "profile_id": profile_dict["id"]},
                        )
                    elif action == "archive":
                        watch.is_active = False
                        watch.is_archived = True
                        logger.info(
                            "post-action: archive watch",
                            extra={"watch_id": wid_str, "profile_id": profile_dict["id"]},
                        )
                    elif action == "reduce_frequency":
                        new_cfg = {
                            **(wi.default_schedule_config or {}),
                            "interval": "1d",
                        }
                        wi.default_schedule_config = new_cfg
                        audit(
                            session,
                            EventType.WATCHED_ITEM_THROTTLED,
                            watched_item_id=str(wi.id),
                            triggering_watch_id=wid_str,
                            new_interval="1d",
                        )
                        logger.info(
                            "post-action: reduce frequency on WatchedItem",
                            extra={
                                "watched_item_id": str(wi.id),
                                "triggering_watch_id": wid_str,
                                "profile_id": profile_dict["id"],
                            },
                        )
                    if orm_profile is not None:
                        orm_profile.is_active = False

            # Re-filter children to those still active+non-archived after
            # per-Watch post-actions.
            active_children = [w for w in children if w.is_active and not w.is_archived]
            if not active_children:
                continue

            # A WatchedItem is due iff its last_checked_at is NULL (never checked)
            # or any child Watch's resolved schedule is overdue.
            due_now = False
            if wi.last_checked_at is None:
                due_now = True
            else:
                for watch in active_children:
                    profiles_orm = profiles_by_watch.get(str(watch.id), [])
                    profiles = (
                        [
                            {
                                "id": str(p.id),
                                "profile_type": p.profile_type,
                                "reference_date": p.reference_date,
                                "date_range_start": p.date_range_start,
                                "date_range_end": p.date_range_end,
                                "rules": p.rules,
                                "post_action": p.post_action,
                                "is_active": p.is_active,
                            }
                            for p in profiles_orm
                        ]
                        if profiles_orm
                        else None
                    )
                    next_due = compute_next_check(
                        schedule_config=resolved_schedule_config(watch),
                        last_checked_at=wi.last_checked_at,
                        now=now,
                        profiles=profiles,
                    )
                    if next_due <= now:
                        due_now = True
                        break

            if due_now:
                logger.info(
                    "scheduling watched_item",
                    extra={"watched_item_id": str(wi.id)},
                )
                await check_watched_item.configure().defer_async(watched_item_id=str(wi.id))
                deferred += 1

        await session.commit()

    if deferred:
        logger.info("schedule_tick deferred checks", extra={"count": deferred})
