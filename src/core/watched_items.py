"""WatchedItem lifecycle business rules shared by the API and dashboard (#228).

``set_watched_item_active`` is the single owner of the pause/resume semantics:
the archived guard, the resume-while-suspended guard, and the dedicated
``WATCHED_ITEM_PAUSED`` / ``WATCHED_ITEM_RESUMED`` audit events. Route layers
translate the raised domain errors into their own transport (409 vs OOB flash)
and own the commit.
"""

from urllib.parse import urlparse, urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domains import domain_name_for_url
from src.core.models.audit_log import EventType, audit
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.scheduling.cadence import parse_interval


def set_item_schedule_interval(watched_item: WatchedItem, interval: str | None) -> None:
    """Set (or clear) the operator's item-tier cadence, releasing any throttle.

    The single owner of the item cadence write, shared by the API PATCH and the
    dashboard's inline interval field — because both must do the *second* half:
    releasing ``throttle_floor_interval``.

    Why the release belongs here (#254 CR-1). The ``reduce_frequency``
    post-action used to write ``default_schedule_config`` directly, so editing
    the interval was itself the way out of a throttle. Moving the throttle to a
    floor kept it safe from reconciliation but took the escape hatch with it: a
    floor nothing clears means one temporal profile firing caps an item at 1d
    permanently, with an operator watching their 30m edit have no effect. An
    automatic path must not create state a human cannot undo.

    Only an operator releases it. Reconciliation deliberately does not — the
    registry has no opinion on mechanism, which is the entire reason the floor is
    a column of its own.

    ``interval`` of ``None`` or empty clears the item tier back to inherited;
    a value is validated (raises ``ValueError``) before anything is written, so a
    rejected edit leaves the floor alone too.
    """
    if not interval:
        watched_item.default_schedule_config = None
    else:
        parse_interval(interval)  # raises ValueError on a bad shape — before any write
        watched_item.default_schedule_config = {
            **(watched_item.default_schedule_config or {}),
            "interval": interval,
        }
    watched_item.throttle_floor_interval = None


def derive_watched_item_name(url: str) -> str:
    """A legible placeholder name derived from a WatchedItem's URL.

    Neither remaining creation path carries a name of its own.
    ``RegistryAnnouncementState`` has no ``name`` field — its grain is registry
    state, not presentation — and the POST route stopped being able to borrow
    the InfoItem's when the Archiver SDK went (#254). ``WatchedItem.name`` is
    NOT NULL, so host + path is the most informative thing derivable from what
    is actually in hand, and it is deliberately not invented registry data: an
    operator can still set a real name, and reconciliation never overwrites one.
    """
    parts = urlsplit(url)
    name = f"{parts.netloc}{parts.path}".rstrip("/") or url
    return name[:255]


def resolve_watch_target(url: str) -> tuple[str, str | None, WatchHealthStatus]:
    """``(effective_url, domain_name, health_status)`` for an operator URL edit.

    **One caller since #251**: the dashboard's
    ``POST /watched-items/{id}/effective-url``. Creates no longer use this —
    they take the URL from Archiver, which is authoritative for it, and start
    ``UNKNOWN`` so a steady-state redirect stays audit-only.

    Nothing here touches the network (the Phase-4 async-create design, #241):
    the submitted URL *is* the effective URL until the first fact's
    ``final_url`` proves otherwise, the domain is its hostname (the same
    ``urlparse().hostname`` derivation the old inline probe used, so the
    domain-keying invariant holds), and the item re-enters ``PROBING`` — this
    is now the only producer of that state. The apply path
    (``apply_fetch_blob``) resolves the redirect, re-derives the domain, and
    clears the state to OK/ERROR.

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
