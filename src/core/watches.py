"""Watch creation service — shared logic used by both API and dashboard routes."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from information_client import InformationClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.info_resolver import resolve_primary
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watch import ContentType, Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.probe import ProbeResult

logger = get_logger(__name__)


async def resolve_watch_url(watch: Watch, client: InformationClient) -> str:
    """Resolve a watch's current target URL from the primary InfoSpec.

    Used at notification/event-emission time so ``watch_url`` reflects the
    operator's current spec, not a stale value. Caller passes the SDK client
    explicitly to keep the registry lookup at the request boundary.
    """
    resolved = await resolve_primary(client, str(watch.info_item_id))
    return resolved.document["target"]["url"]


async def create_watch(
    session: AsyncSession,
    probe_fn: Callable[[str], Awaitable[ProbeResult]],
    info_client: InformationClient,
    *,
    name: str,
    info_item_id: str,
    content_type: str | ContentType,
    schedule_config: dict | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Watch:
    """Create a new Watch bound to *info_item_id*.

    Resolves the URL from the InfoItem's primary InfoSpec via the SDK, probes
    it to populate ``effective_url`` / ``effective_domain``, upserts the
    Domain row, and persists the Watch.

    Raises whatever the SDK raises (``NotFound``, ``httpx.ConnectError``,
    ``ServerError``, ``AuthError``, ``ValidationError``) — translation to
    HTTP status codes is the route layer's concern.

    Probe failures (``httpx.HTTPError``) propagate to the caller — no watch
    or domain is created.
    """
    schedule_config = schedule_config if schedule_config is not None else {}

    # 1. Resolve the primary InfoSpec — also serves as InfoItem-existence check.
    #    NotFound covers both "InfoItem absent" and "InfoItem has no active spec";
    #    ServerError / AuthError / httpx.* propagate to the route handler.
    resolved = await resolve_primary(info_client, info_item_id)
    url = resolved.document["target"]["url"]

    # 2. Probe the URL — establishes effective_url / effective_domain and
    #    fails fast on connection errors. httpx.HTTPError propagates.
    probe_result = await probe_fn(url)

    # 3. Upsert domain — insert with defaults if new, leave config intact if
    #    existing. Guard against TOCTOU race: concurrent inserts may both
    #    pass the scalar_one_or_none() check and hit the unique constraint.
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
        info_item_id=ULID.from_str(info_item_id),
        content_type=content_type,
        schedule_config=schedule_config,
        effective_url=probe_result.effective_url,
        effective_domain=probe_result.effective_domain,
        description=description,
        tags=tags,
    )
    session.add(watch)
    await session.flush()  # populate watch.id before audit

    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        info_item_id=info_item_id,
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
            # At create-time the resolved-spec URL *is* the new watch's URL.
            watch_url=url,
            occurred_at=datetime.now(UTC),
        ),
    )
    await session.commit()
    await session.refresh(watch)
    return watch
