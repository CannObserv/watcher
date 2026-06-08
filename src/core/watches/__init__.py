"""Watch creation service — WatchedItem-first model (#185 Phase A step 7).

create_watch now accepts watched_item_id directly. The WatchedItem must
already exist with its effective_url set. No Archiver SDK calls at Watch-create
time.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.watch import ContentType, Watch
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications

logger = get_logger(__name__)


async def create_watch(
    session: AsyncSession,
    *,
    name: str,
    watched_item_id: str | ULID,
    content_type: str | ContentType | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Watch:
    """Create a Watch under an existing WatchedItem.

    The WatchedItem must already exist with its effective_url populated.

    Raises:
        ValueError — watched_item_id not found.
    """
    wi_ulid = ULID.from_str(str(watched_item_id))
    wi = await session.get(WatchedItem, wi_ulid)
    if wi is None:
        raise ValueError(f"WatchedItem {watched_item_id} not found")

    watch = Watch(
        name=name,
        watched_item_id=wi.id,
        content_type=content_type,
        description=description,
        tags=tags,
    )
    session.add(watch)
    await session.flush()

    watch_url = wi.effective_url or f"watch:{watch.id}"

    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        watched_item_id=str(wi.id),
        url=watch_url,
        content_type=str(content_type) if content_type is not None else None,
    )
    await dispatch_event_notifications(
        session=session,
        event=WatchEvent(
            event_type=WatchEventType.WATCH_CREATED,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=watch_url,
            occurred_at=datetime.now(UTC),
        ),
    )
    await session.commit()
    await session.refresh(watch)
    return watch
