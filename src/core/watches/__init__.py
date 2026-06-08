"""Watch creation service — InfoItem-first model (#160, #185 Phase A)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from archiver_client import ArchiverClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watch import ContentType, Watch
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.probe import ProbeResult
from src.core.watches.info_item_fetch import fetch_info_item_bindings

logger = get_logger(__name__)


async def resolve_watch_url(watch: Watch, client: ArchiverClient) -> str:
    """Resolve the operator-facing URL for a Watch via the InfoItem's primary binding.

    Deprecated: prefer ``watch.watched_item.effective_url`` which is set at
    Watch-create time without a round-trip to Archiver.
    """
    info_item_id = str(watch.watched_item.info_item_id) if watch.watched_item else None
    if not info_item_id:
        return f"watch:{watch.id}"
    bindings = await fetch_info_item_bindings(client, info_item_id)
    return bindings.primary_url


async def _get_or_create_watched_item(
    session: AsyncSession, *, info_item_id: ULID, fallback_name: str
) -> WatchedItem:
    """Look up or create the WatchedItem for an InfoItem.

    The WatchedItem is 1:1 with the InfoItem (uniqueness constraint on
    info_item_id). SELECT-then-INSERT without a savepoint: concurrent
    Watch-creation calls on the same InfoItem (e.g. two operators racing on
    the dashboard, or an API client + a UI submission) raise IntegrityError
    here and propagate to the caller. The check-cycle path (which only reads
    WatchedItems) is unaffected. Acceptable for v1 — Watch creation is rare
    and operator-driven; harden with `begin_nested()` + retry if a real race
    surfaces.

    Emits a WATCHED_ITEM_CREATED audit row with ``source="auto_create"`` only
    when a new row is inserted, so consumers can distinguish operator-driven
    creates from auto-create.
    """
    existing = (
        await session.execute(select(WatchedItem).where(WatchedItem.info_item_id == info_item_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    wi = WatchedItem(info_item_id=info_item_id, name=fallback_name)
    session.add(wi)
    await session.flush()
    audit(
        session,
        EventType.WATCHED_ITEM_CREATED,
        watched_item_id=str(wi.id),
        info_item_id=str(info_item_id),
        name=fallback_name,
        source="auto_create",
    )
    return wi


async def create_watch(
    session: AsyncSession,
    probe_fn: Callable[[str], Awaitable[ProbeResult]],
    info_client: ArchiverClient,
    *,
    name: str,
    info_item_id: str,
    content_type: str | ContentType | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Watch:
    """Create a Watch on an InfoItem's primary content.

    Steps:
    1. Resolve the InfoItem's primary URL via fetch_info_item_bindings.
    2. Probe the URL → effective_url / effective_domain (redirect-detection).
    3. Upsert the Domain row.
    4. Get-or-create the WatchedItem for this InfoItem.
    5. Set watched_item.effective_url from the probe (first Watch wins).
    6. Persist Watch + audit + dispatch WATCH_CREATED event.

    Raises:
        archiver_client.NotFound — info_item_id (or its primary binding) unknown.
        httpx.HTTPError — probe failed.
    """
    bindings = await fetch_info_item_bindings(info_client, info_item_id)

    probe_result = await probe_fn(bindings.primary_url)

    # Upsert Domain via a savepoint so a concurrent insert raising IntegrityError
    # doesn't roll back the enclosing transaction (which contains the in-flight
    # Watch + WatchedItem inserts).
    domain_stmt = select(Domain).where(Domain.name == probe_result.effective_domain)
    if not (await session.execute(domain_stmt)).scalar_one_or_none():
        try:
            async with session.begin_nested():
                session.add(
                    Domain(
                        name=probe_result.effective_domain,
                        min_interval=DEFAULT_MIN_INTERVAL,
                        max_concurrency=DEFAULT_MAX_CONCURRENCY,
                        current_interval=DEFAULT_MIN_INTERVAL,
                    )
                )
        except IntegrityError:
            # Concurrent insert won the race. The savepoint auto-rolls back;
            # the enclosing transaction stays intact.
            pass

    watched_item = await _get_or_create_watched_item(
        session,
        info_item_id=ULID.from_str(info_item_id),
        fallback_name=name,
    )

    # Populate domain_name + effective_url on the WatchedItem if not yet set
    # (first Watch wins).
    if watched_item.domain_name is None and probe_result.effective_domain:
        watched_item.domain_name = probe_result.effective_domain
        await session.flush()
    if not watched_item.effective_url and probe_result.effective_url:
        watched_item.effective_url = probe_result.effective_url
        await session.flush()

    watch = Watch(
        name=name,
        watched_item_id=watched_item.id,
        content_type=content_type,
        description=description,
        tags=tags,
    )
    session.add(watch)
    await session.flush()

    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        info_item_id=info_item_id,
        watched_item_id=str(watched_item.id),
        url=bindings.primary_url,
        content_type=str(content_type) if content_type is not None else None,
        effective_url=probe_result.effective_url,
        domain_name=probe_result.effective_domain,
    )
    await dispatch_event_notifications(
        session=session,
        event=WatchEvent(
            event_type=WatchEventType.WATCH_CREATED,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=bindings.primary_url,
            occurred_at=datetime.now(UTC),
        ),
    )
    await session.commit()
    await session.refresh(watch)
    return watch
