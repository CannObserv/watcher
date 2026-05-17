"""Live-inheritance resolvers — Watch override → WatchedItem default → system default.

Per design Section 4 (#160). Resolution is performed at read time so edits to a
WatchedItem propagate immediately to all child Watches that do not override the
field. Tags merge additively (union); scalars override. ``resolved_notification_dispatches``
returns the Approach B union of WatchedItem templates + per-Watch configs as a
single uniform list of dispatch candidates.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.watch import ContentType, Watch
from src.core.models.watched_item_notification_template import (
    WatchedItemNotificationTemplate,
)

SYSTEM_DEFAULT_SCHEDULE_CONFIG: dict = {"interval": "1d"}
SYSTEM_DEFAULT_CONTENT_TYPE: ContentType = ContentType.HTML


def resolved_schedule_config(watch: Watch) -> dict:
    """Schedule lives on WatchedItem only — Watch has no per-row override.

    Distinguishes `None` (no override set) from `{}` (explicitly empty override).
    Empty dict passes through; `None` falls back to the system default.
    """
    wi = watch.watched_item
    if wi is not None and wi.default_schedule_config is not None:
        return wi.default_schedule_config
    return SYSTEM_DEFAULT_SCHEDULE_CONFIG


def resolved_content_type(watch: Watch) -> ContentType:
    """Resolve content_type: Watch override → WatchedItem default → system default."""
    if watch.content_type is not None:
        return watch.content_type
    wi = watch.watched_item
    if wi is not None and wi.default_content_type is not None:
        return wi.default_content_type
    return SYSTEM_DEFAULT_CONTENT_TYPE


def resolved_tags(watch: Watch) -> list[str]:
    """Additive merge: WatchedItem.default_tags ∪ Watch.tags, sorted."""
    wi_tags = (watch.watched_item.default_tags if watch.watched_item else None) or []
    own = watch.tags or []
    return sorted(set(wi_tags) | set(own))


@dataclass
class ResolvedNotificationDispatch:
    """A single notification dispatch target, normalised across both source tables.

    `source` discriminates origin for logging/audit only — the notifier-facing
    dispatcher treats both kinds identically. ``source_id`` is the row id from
    the originating table (`WatchedItemNotificationTemplate.id` or
    `WatchNotificationConfig.id`), stringified.
    """

    source: str  # "watched_item_template" | "watch_config"
    source_id: str
    channel_hint: str
    events: list[str]
    content_config: dict | None
    remote_channel_id: str | None


async def resolved_notification_dispatches(
    session: AsyncSession,
    watch: Watch,
    *,
    event_type: str | None = None,
) -> list[ResolvedNotificationDispatch]:
    """Approach B union of WatchedItem templates + per-Watch configs.

    Per design Section 4.3. Returns one entry per active row from either source.
    No suppression semantics in v1 — every active row that matches the event
    filter (when supplied) becomes a dispatch candidate.

    `event_type` (optional) filters both sets to rows whose `events` array
    contains the given WatchEventType code. When ``None``, all active rows are
    returned regardless of event subscription. De-duplication by row id is not
    needed: the two tables have disjoint primary-key namespaces (different
    parent FKs), so the same row cannot appear in both sets.
    """
    template_q = select(WatchedItemNotificationTemplate).where(
        WatchedItemNotificationTemplate.watched_item_id == watch.watched_item_id,
        WatchedItemNotificationTemplate.is_active.is_(True),
    )
    config_q = select(WatchNotificationConfig).where(
        WatchNotificationConfig.watch_id == watch.id,
        WatchNotificationConfig.is_active.is_(True),
    )
    if event_type is not None:
        template_q = template_q.where(WatchedItemNotificationTemplate.events.contains([event_type]))
        config_q = config_q.where(WatchNotificationConfig.events.contains([event_type]))

    template_rows = (await session.execute(template_q)).scalars().all()
    config_rows = (await session.execute(config_q)).scalars().all()

    resolved: list[ResolvedNotificationDispatch] = []
    for tpl in template_rows:
        resolved.append(
            ResolvedNotificationDispatch(
                source="watched_item_template",
                source_id=str(tpl.id),
                channel_hint=tpl.channel_hint,
                events=list(tpl.events),
                content_config=tpl.content_config,
                remote_channel_id=tpl.remote_channel_id,
            )
        )
    for cfg in config_rows:
        resolved.append(
            ResolvedNotificationDispatch(
                source="watch_config",
                source_id=str(cfg.id),
                channel_hint=cfg.channel_hint,
                events=list(cfg.events),
                content_config=cfg.content_config,
                remote_channel_id=cfg.remote_channel_id,
            )
        )
    return resolved
