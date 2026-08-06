"""WatchedItem lifecycle business rules shared by the API and dashboard (#228).

``set_watched_item_active`` is the single owner of the pause/resume semantics:
the archived guard, the resume-while-suspended guard, and the dedicated
``WATCHED_ITEM_PAUSED`` / ``WATCHED_ITEM_RESUMED`` audit events. Route layers
translate the raised domain errors into their own transport (409 vs OOB flash)
and own the commit.
"""

from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domains import domain_name_for_url
from src.core.models.audit_log import EventType, audit
from src.core.models.watched_item import WatchedItem, WatchHealthStatus


def resolve_watch_target(url: str) -> tuple[str, str | None, WatchHealthStatus]:
    """``(effective_url, domain_name, health_status)`` for a URL-first create/edit.

    Nothing here touches the network (the Phase-4 async-create design, #241):
    the submitted URL *is* the effective URL until the first fact's
    ``final_url`` proves otherwise, the domain is its hostname (the same
    ``urlparse().hostname`` derivation the old inline probe used, so the
    domain-keying invariant holds), and the item starts ``PROBING``. The apply
    path (``apply_fetch_blob``) resolves the redirect, re-derives the domain,
    and clears the state to OK/ERROR.

    That is what closes the boundaries-charter exception for the *scheduled*
    path: nothing Watcher does on a timer touches an origin. Two
    operator-initiated one-shot probes remain by design (domain create and
    ``POST /api/v1/probe``) — a human action against a URL they just typed,
    not automated traffic.
    """
    # Syntactic validation stays at the boundary (CR-3): a typo'd URL must
    # fail the request, not surface as ERROR health minutes later via a
    # cross-service round trip through Replicator's DLQ.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            f"invalid URL {url!r}: an absolute http(s) URL with a hostname is required"
        )
    return url, domain_name_for_url(url), WatchHealthStatus.PROBING


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
