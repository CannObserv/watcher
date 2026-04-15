"""Watch creation service — shared logic used by both API and dashboard routes."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watch import ContentType, Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.probe import ProbeResult

logger = get_logger(__name__)


async def create_watch(
    session: AsyncSession,
    probe_fn: Callable[[str], Awaitable[ProbeResult]],
    name: str,
    url: str,
    content_type: str | ContentType,
    schedule_config: dict,
    fetch_config: dict,
) -> Watch:
    """Create a new Watch with full probe + domain upsert + audit + notification flow.

    Probes *url* to resolve the effective URL and domain. Raises ``httpx.HTTPError``
    on connection failure — callers should convert this to the appropriate HTTP
    error response or form flash message.

    Domain is upserted (inserted with defaults if new, left intact if existing).
    The audit entry is written after ``flush()`` so ``watch.id`` is guaranteed
    non-null. A ``WATCH_CREATED`` notification is dispatched before commit.
    """
    # httpx.HTTPError propagates to the caller — no watch or domain created.
    probe_result = await probe_fn(url)

    # Upsert domain — insert with defaults if new, leave config intact if existing.
    # Guard against TOCTOU race: concurrent inserts may both pass the
    # scalar_one_or_none() check and hit the unique constraint simultaneously.
    domain_stmt = select(Domain).where(Domain.name == probe_result.effective_domain)
    domain_result = await session.execute(domain_stmt)
    if not domain_result.scalar_one_or_none():
        try:
            session.add(
                Domain(
                    name=probe_result.effective_domain,
                    min_interval=DEFAULT_MIN_INTERVAL,
                    max_concurrency=DEFAULT_MAX_CONCURRENCY,
                    current_interval=DEFAULT_MIN_INTERVAL,
                )
            )
            await session.flush()
        except IntegrityError:
            await session.rollback()

    watch = Watch(
        name=name,
        url=url,
        content_type=content_type,
        fetch_config=fetch_config,
        schedule_config=schedule_config,
        effective_url=probe_result.effective_url,
        effective_domain=probe_result.effective_domain,
    )
    session.add(watch)
    await session.flush()  # populate watch.id before audit

    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        url=url,
        content_type=str(content_type),
        effective_url=probe_result.effective_url,
        effective_domain=probe_result.effective_domain,
    )
    await dispatch_event_notifications(
        session=session,
        event=WatchEvent(
            event_type=WatchEventType.WATCH_CREATED,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=watch.url,
            occurred_at=datetime.now(UTC),
        ),
    )
    await session.commit()
    await session.refresh(watch)
    return watch
