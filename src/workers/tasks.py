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
from sqlalchemy import select
from ulid import ULID

from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.rate_limiter import get_rate_limiter
from src.core.registry import ServiceRegistry, get_registry
from src.core.scheduler import compute_next_check, evaluate_post_actions
from src.core.utils import watched_item_event_base_metadata
from src.core.watches.resolution import resolved_schedule_config
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
    (both success and fetch-failure paths). WATCH_ERROR / WATCH_RECOVERED
    events are dispatched once per WatchedItem when its health transitions;
    CHANGE_DETECTED is dispatched inline by the pipeline.
    """
    reg = registry if registry is not None else get_registry()
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
            # Track health transition on WatchedItem; dispatch WATCH_ERROR once
            # for the WatchedItem if it transitions to ERROR (#191).
            previous_health = watched_item.health_status
            watched_item.health_status = WatchHealthStatus.ERROR
            watched_item.last_checked_at = now
            await session.commit()

            if previous_health != WatchHealthStatus.ERROR:
                error_event = WatchEvent(
                    event_type=WatchEventType.WATCH_ERROR,
                    watch_id=str(watched_item.id),
                    watch_name=watched_item.name,
                    watch_url=watched_item.effective_url or url,
                    occurred_at=now,
                    metadata={
                        "status_code": fetch_result.status_code,
                        **watched_item_event_base_metadata(watched_item),
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

        # Recovery: dispatch WATCH_RECOVERED once when the WatchedItem
        # transitions ERROR → OK (#191).
        if previous_health == WatchHealthStatus.ERROR:
            recovery_event = WatchEvent(
                event_type=WatchEventType.WATCH_RECOVERED,
                watch_id=str(watched_item.id),
                watch_name=watched_item.name,
                watch_url=watched_item.effective_url or url,
                occurred_at=now,
                metadata=watched_item_event_base_metadata(watched_item),
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
        return [
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
                        logger.info(
                            "post-action: deactivate watched_item",
                            extra={"watched_item_id": str(wi.id), "profile_id": profile_dict["id"]},
                        )
                    elif action == "archive":
                        wi.is_active = False
                        wi.archived_at = now
                        logger.info(
                            "post-action: archive watched_item",
                            extra={"watched_item_id": str(wi.id), "profile_id": profile_dict["id"]},
                        )
                    elif action == "reduce_frequency":
                        wi.default_schedule_config = {
                            **(wi.default_schedule_config or {}),
                            "interval": "1d",
                        }
                        audit(
                            session,
                            EventType.WATCHED_ITEM_THROTTLED,
                            watched_item_id=str(wi.id),
                            new_interval="1d",
                        )
                        logger.info(
                            "post-action: reduce frequency on watched_item",
                            extra={"watched_item_id": str(wi.id), "profile_id": profile_dict["id"]},
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

    if deferred:
        logger.info("schedule_tick deferred checks", extra={"count": deferred})
