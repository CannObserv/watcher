"""Procrastinate task wrappers: check_watch and schedule_tick."""

from datetime import UTC, datetime
from urllib.parse import urlparse

import procrastinate
from sqlalchemy import or_, select
from ulid import ULID

from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch
from src.core.rate_limiter import get_rate_limiter
from src.core.registry import ServiceRegistry
from src.core.scheduler import compute_next_check, evaluate_post_actions
from src.core.storage import STORAGE_BASE_DIR, LocalStorage
from src.workers import bp
from src.workers.notify import dispatch_change_notifications
from src.workers.pipeline import _persist_backoff, _run_check_pipeline

logger = get_logger(__name__)

# Shared resources — lazy-initialized on first use to avoid binding to an
# event loop at import time (important for DomainRateLimiter's asyncio primitives).
_registry: ServiceRegistry | None = None


def get_registry() -> ServiceRegistry:
    """Return the shared ServiceRegistry, creating it on first call."""
    global _registry
    if _registry is None:
        _registry = ServiceRegistry()
    return _registry


# --- Procrastinate task wrappers ---


@bp.task(
    name="check_watch",
    queue="default",
    retry=procrastinate.RetryStrategy(
        max_attempts=3,
        exponential_wait=5,
        retry_exceptions={ConnectionError, TimeoutError},
    ),
)
async def check_watch(watch_id: str, registry: ServiceRegistry | None = None) -> dict:
    """Fetch and check a single watch for changes."""
    reg = registry if registry is not None else get_registry()
    async with get_session_factory()() as session:
        watch = await session.get(Watch, ULID.from_str(watch_id))
        if not watch or not watch.is_active:
            logger.warning("watch not found or inactive", extra={"watch_id": watch_id})
            return {"skipped": True}

        # Fetch with rate limiting — only pass fetcher-relevant config keys
        fetch_config = {
            k: v for k, v in (watch.fetch_config or {}).items() if k in ("headers", "timeout")
        }
        # Use effective_domain if resolved; fall back to URL parsing for old watches
        rate_limit_domain = watch.effective_domain or urlparse(watch.url).hostname or watch.url

        async with get_rate_limiter().acquire_for_domain(rate_limit_domain):
            fetch_result = await reg.get_fetcher().fetch(watch.url, config=fetch_config)

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
            await session.commit()
            return {"error": f"HTTP {fetch_result.status_code}"}

        # Run pipeline
        storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=fetch_result.content,
            fetcher_used=fetch_result.fetcher_used,
            fetch_duration_ms=fetch_result.duration_ms,
            storage=storage,
            session=session,
        )

        # Commit pipeline results (snapshot/change records) before dispatching
        # notifications. This ensures snapshot/change data is persisted even if
        # notification dispatch fails.
        watch.last_checked_at = datetime.now(UTC)
        await session.commit()

        # Dispatch notifications in a separate transaction scope
        if result.get("change_id"):
            await dispatch_change_notifications(
                session=session,
                watch=watch,
                change_id=result["change_id"],
                change_metadata=result.get("change_metadata", {}),
            )
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
        stmt = select(Watch).where(
            Watch.is_active.is_(True),
            or_(
                Watch.last_checked_at.is_(None),
                Watch.last_checked_at < now,
            ),
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
                    if action in ("deactivate", "archive"):
                        watch.is_active = False
                        logger.info(
                            f"post-action: {action} watch",
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
