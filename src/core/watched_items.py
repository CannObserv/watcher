"""WatchedItem lifecycle business rules shared by the API and dashboard (#228).

``set_watched_item_active`` is the single owner of the pause/resume semantics:
the archived guard, the resume-while-suspended guard, and the dedicated
``WATCHED_ITEM_PAUSED`` / ``WATCHED_ITEM_RESUMED`` audit events. Route layers
translate the raised domain errors into their own transport (409 vs OOB flash)
and own the commit.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.audit_log import EventType, audit
from src.core.models.watched_item import WatchedItem


class ArchivedItemActivationError(Exception):
    """is_active may not change while archived — archive/restore owns activation."""


class SuspendedDomainResumeError(Exception):
    """An item cannot resume while its domain is suspended (kill-switch parity)."""


def set_watched_item_active(
    session: AsyncSession, wi: WatchedItem, *, active: bool, source: str
) -> bool:
    """Apply a pause/resume transition to ``wi``, enforcing the shared guards.

    Returns True when the value changed (an audit event was emitted), False on
    a no-op. Raises :class:`ArchivedItemActivationError` for any attempt while
    archived (even a no-op — restore owns activation), and
    :class:`SuspendedDomainResumeError` when resuming while ``domain_suspended``.
    Does not commit; the caller owns the transaction.
    """
    if wi.archived_at is not None:
        raise ArchivedItemActivationError(
            "WatchedItem is archived; activation is controlled by restore"
        )
    if active and wi.domain_suspended:
        raise SuspendedDomainResumeError("Cannot resume while the domain is suspended")
    if wi.is_active == active:
        return False
    wi.is_active = active
    audit(
        session,
        EventType.WATCHED_ITEM_RESUMED if active else EventType.WATCHED_ITEM_PAUSED,
        watched_item_id=str(wi.id),
        source=source,
    )
    return True
