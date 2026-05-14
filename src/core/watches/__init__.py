"""Watch creation service — shared logic used by both API and dashboard routes."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from archiver_client import ArchiverClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID  # used in ULID.from_str

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watch import ContentType, Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.probe import ProbeResult

logger = get_logger(__name__)


async def resolve_watch_url(watch: Watch, client: ArchiverClient) -> str:
    """Resolve a watch's current target URL from the primary InfoSpec.

    Used at notification/event-emission time so ``watch_url`` reflects the
    operator's current spec, not a stale value. Caller passes the SDK client
    explicitly to keep the registry lookup at the request boundary.

    Resolves via ``info_source_id`` once Phase 5 SDK support lands; for now
    falls back to the InfoSource's URL from the source_spec.
    """
    source = await client.get_info_source(str(watch.info_source_id))
    return source.source_spec.additional_properties["target"]["url"]


async def create_watch(
    session: AsyncSession,
    probe_fn: Callable[[str], Awaitable[ProbeResult]],
    info_client: ArchiverClient,
    *,
    name: str,
    info_item_id: str,
    content_type: str | ContentType,
    schedule_config: dict | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    info_source_id: str | None = None,
) -> Watch:
    """Create a new Watch.

    Resolves the target URL from the InfoSource via ``get_info_source``, probes
    it to populate ``effective_url`` / ``effective_domain``, upserts the
    Domain row, and persists the Watch.

    *info_source_id* is required (Phase 5+). *info_item_id* is retained on the
    function signature for call-site compatibility during the cutover window;
    Tasks 7.x will remove it.

    Raises whatever the SDK raises (``NotFound``, ``httpx.ConnectError``,
    ``ServerError``, ``AuthError``, ``ValidationError``) — translation to
    HTTP status codes is the route layer's concern.

    Probe failures (``httpx.HTTPError``) propagate to the caller — no watch
    or domain is created.
    """
    schedule_config = schedule_config if schedule_config is not None else {}

    if info_source_id is None:
        raise ValueError("info_source_id is required (Phase 5+)")

    # 1. Resolve the target URL from the InfoSource (Phase 5+).
    #    NotFound / ServerError / AuthError / httpx.* propagate to the route handler.
    #    Fragments have no target.url — walk up parent chain to find the root URL.
    source = await info_client.get_info_source(info_source_id)
    spec_props = source.source_spec.additional_properties
    url = spec_props.get("target", {}).get("url")
    while url is None and source.parent_info_source_id is not None:
        source = await info_client.get_info_source(str(source.parent_info_source_id))
        url = source.source_spec.additional_properties.get("target", {}).get("url")
    if url is None:
        raise ValueError(
            f"InfoSource {info_source_id}: no target.url found on source or any ancestor"
        )

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

    watch_kwargs: dict = {
        "name": name,
        "info_source_id": ULID.from_str(info_source_id),
        "content_type": content_type,
        "schedule_config": schedule_config,
        "effective_url": probe_result.effective_url,
        "effective_domain": probe_result.effective_domain,
        "description": description,
        "tags": tags,
    }

    watch = Watch(**watch_kwargs)
    session.add(watch)
    await session.flush()  # populate watch.id before audit

    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        info_source_id=info_source_id,
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
