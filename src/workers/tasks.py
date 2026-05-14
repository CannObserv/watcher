"""Procrastinate task wrappers: check_watch and schedule_tick."""

from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import procrastinate
from archiver_client import NotFound
from archiver_client.defaults import fetch_render, fetch_timeout_seconds
from archiver_client.errors import ServerError
from sqlalchemy import or_, select
from ulid import ULID

from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch, WatchHealthStatus
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.rate_limiter import get_rate_limiter
from src.core.registry import ServiceRegistry, get_registry
from src.core.scheduler import compute_next_check, evaluate_post_actions
from src.core.sources.resolver import resolve_root_sources_with_children
from src.core.utils import format_utc_iso
from src.workers import bp
from src.workers.notify import dispatch_event_notifications
from src.workers.pipeline import _maybe_decay_backoff, _persist_backoff, _run_check_pipeline

logger = get_logger(__name__)


def _watch_base_metadata(watch: Watch) -> dict:
    """Common metadata fields added to all WatchEvents for content-builder use."""
    meta: dict = {}
    if watch.effective_domain:
        meta["effective_domain"] = watch.effective_domain
    interval = (watch.schedule_config or {}).get("interval")
    if interval:
        meta["check_interval"] = interval
    if watch.last_changed_at:
        meta["last_changed_at"] = format_utc_iso(watch.last_changed_at)
    if watch.tags:
        meta["tags"] = watch.tags
    if watch.description:
        meta["description"] = watch.description
    return meta


# --- Procrastinate task wrappers ---


@bp.task(
    name="check_watch",
    queue="default",
    retry=procrastinate.RetryStrategy(
        max_attempts=3,
        exponential_wait=5,
        # Builtins cover fetcher errors; httpx + ServerError cover the
        # ArchiverClient SDK (none of which subclass the Python builtins,
        # so they would otherwise fail the task on first attempt).
        # AuthError, NotFound, and ValidationError are NOT retried —
        # those are operator-fixable; they propagate loud or are handled
        # explicitly downstream.
        retry_exceptions={
            ConnectionError,
            TimeoutError,
            httpx.ConnectError,
            httpx.TimeoutException,
            ServerError,
        },
    ),
)
async def check_watch(watch_id: str, registry: ServiceRegistry | None = None) -> dict:
    """Fetch and check a single watch for changes."""
    reg = registry if registry is not None else get_registry()
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(Watch, Domain)
                .outerjoin(Domain, Domain.name == Watch.effective_domain)
                .where(Watch.id == ULID.from_str(watch_id))
            )
        ).one_or_none()
        if not row:
            logger.warning("watch not found", extra={"watch_id": watch_id})
            return {"skipped": True}
        watch, domain = row

        if not watch.is_active:
            logger.warning("watch inactive", extra={"watch_id": watch_id})
            return {"skipped": True}

        if domain and not domain.is_active:
            logger.warning(
                "domain inactive, skipping watch",
                extra={"watch_id": watch_id, "domain": watch.effective_domain},
            )
            return {"skipped": True}

        # Resolve the root InfoSource; URL + fetch defaults come from the
        # source_spec, not the Watch row.
        info_client = reg.get_archiver_client()
        try:
            resolved = await resolve_root_sources_with_children(
                info_client, str(watch.info_source_id)
            )
        except NotFound:
            # Operator-fixable: the InfoSource was deleted out from under the
            # watch. Skip until operator action; do not retry.
            logger.error(
                "info_source missing for watch — skipping until operator action",
                extra={"watch_id": watch_id, "info_source_id": str(watch.info_source_id)},
            )
            return {"skipped": True, "reason": "info_item_missing"}
        # Other SDK errors (httpx.ConnectError, httpx.TimeoutException,
        # ServerError) propagate to Procrastinate's RetryStrategy. AuthError
        # and ValidationError propagate loud (operator-fixable).

        url = resolved.url
        # NB: fetch_render is resolved but the current HttpFetcher does not
        # accept a `render` flag — JS rendering is Phase 3 work. Pass only
        # `timeout` to stay within the existing fetcher contract.
        # TODO Phase 3: render flag plumbing into fetcher.
        _render = fetch_render(resolved.source_spec)  # noqa: F841 — reserved for Phase 3
        fetch_timeout = fetch_timeout_seconds(resolved.source_spec)
        fetch_config = {"timeout": fetch_timeout}

        rate_limit_domain = watch.effective_domain or urlparse(url).hostname or url

        async with get_rate_limiter().acquire_for_domain(rate_limit_domain):
            fetch_result = await reg.get_fetcher().fetch(url, config=fetch_config)

        if fetch_result.status_code == 429:
            new_interval = get_rate_limiter().report_rate_limited_for_domain(rate_limit_domain)
            await _persist_backoff(rate_limit_domain, new_interval, session)
            await session.commit()
            raise ConnectionError(f"Rate limited by {rate_limit_domain}")

        if not fetch_result.is_success:
            logger.warning(
                "fetch failed",
                extra={"watch_id": watch_id, "status": fetch_result.status_code},
            )
            audit(
                session,
                EventType.CHECK_FETCH_FAILED,
                watch_id=watch.id,
                status_code=fetch_result.status_code,
            )
            # Detect watch_error state transition (only fire on first failure)
            previous_health = watch.health_status
            watch.health_status = WatchHealthStatus.ERROR
            await session.commit()
            if previous_health != WatchHealthStatus.ERROR:
                error_event = WatchEvent(
                    event_type=WatchEventType.WATCH_ERROR,
                    watch_id=str(watch.id),
                    watch_name=watch.name,
                    watch_url=url,
                    occurred_at=datetime.now(UTC),
                    metadata={
                        "status_code": fetch_result.status_code,
                        **_watch_base_metadata(watch),
                    },
                )
                await dispatch_event_notifications(session=session, event=error_event)
                await session.commit()
            return {"error": f"HTTP {fetch_result.status_code}"}

        # Run pipeline
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=fetch_result.content,
            fetcher_used=fetch_result.fetcher_used,
            fetch_duration_ms=fetch_result.duration_ms,
            session=session,
            resolved=resolved,
            info_client=info_client,
        )

        # Update health + timestamp; commit pipeline + health together.
        previous_health = watch.health_status
        watch.health_status = WatchHealthStatus.OK
        watch.last_checked_at = datetime.now(UTC)
        await session.commit()

        # Check if domain backoff should decay after successful fetch
        _limiter = get_rate_limiter()
        _state = _limiter._domains.get(rate_limit_domain)
        if _state and _state.current_interval > _state.min_interval:
            await _maybe_decay_backoff(rate_limit_domain, _limiter, session)
            await session.commit()

        # Dispatch recovery notification on state transition ERROR → OK
        if previous_health == WatchHealthStatus.ERROR:
            recovery_event = WatchEvent(
                event_type=WatchEventType.WATCH_RECOVERED,
                watch_id=str(watch.id),
                watch_name=watch.name,
                watch_url=url,
                occurred_at=datetime.now(UTC),
                metadata=_watch_base_metadata(watch),
            )
            await dispatch_event_notifications(session=session, event=recovery_event)
            await session.commit()

    # schedule_tick is the sole scheduler — no self-deferral here.
    # This avoids double-deferral races and keeps scheduling logic
    # in one place (important for Phase 4 temporal profiles).
    return result


@bp.periodic(cron="* * * * *")
@bp.task(name="schedule_tick", queue="default")
async def schedule_tick(timestamp: int) -> None:
    """Find active watches due for checking and defer check_watch jobs."""
    now = datetime.now(UTC)

    # Load all active watches that might be due. Can't filter by interval in SQL
    # (per-watch JSONB config), so we load all and filter in Python.
    # Acceptable at 2,000 watches; revisit if scale increases significantly.
    async with get_session_factory()() as session:
        stmt = (
            select(Watch)
            .join(Domain, Domain.name == Watch.effective_domain, isouter=True)
            .where(
                Watch.is_active.is_(True),
                or_(Domain.id.is_(None), Domain.is_active.is_(True)),
                or_(
                    Watch.last_checked_at.is_(None),
                    Watch.last_checked_at < now,
                ),
            )
        )
        result = await session.execute(stmt)
        watches = list(result.scalars().all())

        # Batch-load all active temporal profiles (avoids N+1 per watch)
        tp_stmt = select(TemporalProfile).where(TemporalProfile.is_active.is_(True))
        tp_result = await session.execute(tp_stmt)
        all_profiles_orm = list(tp_result.scalars().all())
        profiles_by_watch: dict[str, list] = {}
        for p in all_profiles_orm:
            profiles_by_watch.setdefault(str(p.watch_id), []).append(p)

        deferred = 0
        for watch in watches:
            watch_id_str = str(watch.id)
            profiles_orm = profiles_by_watch.get(watch_id_str, [])

            # Convert to dicts for scheduler functions
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

            # Evaluate and apply post-event actions
            if profiles:
                actions = evaluate_post_actions(profiles, today=now.date())
                for action_info in actions:
                    action = action_info["action"]
                    profile_dict = action_info["profile"]
                    # Find the ORM profile to deactivate
                    orm_profile = next(
                        (p for p in profiles_orm if str(p.id) == profile_dict["id"]),
                        None,
                    )
                    if action == "deactivate":
                        watch.is_active = False
                        logger.info(
                            "post-action: deactivate watch",
                            extra={"watch_id": watch_id_str, "profile_id": profile_dict["id"]},
                        )
                    elif action == "archive":
                        watch.is_active = False
                        watch.is_archived = True
                        logger.info(
                            "post-action: archive watch",
                            extra={"watch_id": watch_id_str, "profile_id": profile_dict["id"]},
                        )
                    elif action == "reduce_frequency":
                        watch.schedule_config = {**(watch.schedule_config or {}), "interval": "1d"}
                        logger.info(
                            "post-action: reduce frequency",
                            extra={"watch_id": watch_id_str, "profile_id": profile_dict["id"]},
                        )
                    if orm_profile:
                        orm_profile.is_active = False

            # Skip deferred check if watch was deactivated by post-action
            if not watch.is_active:
                continue

            next_due = compute_next_check(
                schedule_config=watch.schedule_config or {},
                last_checked_at=watch.last_checked_at,
                now=now,
                profiles=profiles if profiles else None,
            )
            if next_due <= now:
                logger.info("deferring check", extra={"watch_id": watch_id_str})
                await check_watch.configure().defer_async(watch_id=watch_id_str)
                deferred += 1

        # Commit any post-action changes (deactivated profiles, modified watches)
        await session.commit()

    if deferred:
        logger.info("schedule_tick deferred checks", extra={"count": deferred})
