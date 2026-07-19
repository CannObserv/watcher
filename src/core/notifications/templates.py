"""NotificationTemplate mutation service shared by every CRUD surface (#228).

Owns the mutation + audit-event pairing for template create/update/delete/
duplicate. Callers (API library routes, API item-scoped routes, dashboard
routes) supply their surface-specific audit payload via ``audit_fields`` and
own the commit; the service flushes so generated ids are available.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.audit_log import EventType, audit
from src.core.models.notification_template import NotificationTemplate

#: Fields ``update_template_fields`` may assign. ``visibility`` and the scope
#: refs are immutable post-create on every surface.
MUTABLE_TEMPLATE_FIELDS = frozenset(
    {"title", "channel_hint", "events", "is_active", "remote_channel_id", "content_config"}
)


async def create_template(
    session: AsyncSession,
    *,
    visibility: str,
    title: str,
    channel_hint: str,
    events: list[str],
    domain_name: str | None = None,
    watched_item_id=None,
    content_config: dict | None = None,
    remote_channel_id: str | None = None,
    audit_fields: dict,
) -> NotificationTemplate:
    """Create a template, flush it, and audit ``NOTIFICATION_TEMPLATE_CREATED``."""
    tpl = NotificationTemplate(
        visibility=visibility,
        title=title,
        channel_hint=channel_hint,
        events=events,
        domain_name=domain_name,
        watched_item_id=watched_item_id,
        content_config=content_config,
        remote_channel_id=remote_channel_id,
    )
    session.add(tpl)
    await session.flush()
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        template_id=str(tpl.id),
        **audit_fields,
    )
    return tpl


def update_template_fields(
    session: AsyncSession,
    tpl: NotificationTemplate,
    updates: dict,
    *,
    audit_fields: dict,
) -> NotificationTemplate:
    """Assign the given mutable fields and audit ``NOTIFICATION_TEMPLATE_UPDATED``.

    Only keys present in ``updates`` are touched (``content_config: None`` is a
    valid assignment). Unknown fields raise ``ValueError`` — visibility and the
    scope refs are immutable.
    """
    unknown = set(updates) - MUTABLE_TEMPLATE_FIELDS
    if unknown:
        raise ValueError(f"immutable or unknown template fields: {sorted(unknown)}")
    for field, value in updates.items():
        setattr(tpl, field, value)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_UPDATED,
        template_id=str(tpl.id),
        **audit_fields,
    )
    return tpl


async def delete_template(
    session: AsyncSession,
    tpl: NotificationTemplate,
    *,
    audit_fields: dict,
) -> None:
    """Audit ``NOTIFICATION_TEMPLATE_DELETED`` (id captured first), then delete."""
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_DELETED,
        template_id=str(tpl.id),
        **audit_fields,
    )
    await session.delete(tpl)


async def duplicate_template(
    session: AsyncSession,
    tpl: NotificationTemplate,
    *,
    audit_fields: dict,
) -> NotificationTemplate:
    """Copy a template (title suffixed ``(copy)``), audit CREATED with the new id."""
    return await create_template(
        session,
        visibility=tpl.visibility,
        title=f"{tpl.title} (copy)",
        channel_hint=tpl.channel_hint,
        events=list(tpl.events),
        domain_name=tpl.domain_name,
        watched_item_id=tpl.watched_item_id,
        content_config=tpl.content_config,
        remote_channel_id=tpl.remote_channel_id,
        audit_fields=audit_fields,
    )
