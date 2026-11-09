"""WatchedItem lifecycle business rules for the API (#228; the dashboard went in #377).

``set_watched_item_active`` is the single owner of the pause/resume semantics:
the archived guard, the resume-while-suspended guard, and the dedicated
``WATCHED_ITEM_PAUSED`` / ``WATCHED_ITEM_RESUMED`` audit events. Route layers
translate the raised domain errors into their own transport (409 vs OOB flash)
and own the commit.
"""

from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.audit_log import EventType, audit
from src.core.models.watched_item import WatchedItem


def set_item_schedule_config(watched_item: WatchedItem, config: dict | None) -> None:
    """Set (or clear) the operator's item cadence, releasing any throttle.

    **Inert since F2 (#375).** Neither column moves the resolved cadence any
    more — Archiver announces it — so this writer stores and clears values that
    schedule nothing. It stays until #378 deletes the API; the columns go in
    #380. What follows is why it was built this way.

    The single owner of the item cadence write: the API's
    ``PATCH /api/v1/watched-items/{id}`` (whole ``schedule_config`` dict). The
    dashboard's string-shaped front door to it went with the dashboard (#377).

    Why the throttle release belongs here (#254 CR-1). The ``reduce_frequency``
    post-action used to write ``default_schedule_config`` directly, so editing
    the interval was itself the way out of a throttle. Moving the throttle to a
    floor kept it safe from reconciliation but took the escape hatch with it: a
    floor nothing clears means one temporal profile firing caps an item at 1d
    permanently, with an operator watching their 30m edit have no effect. An
    automatic path must not create state a human cannot undo.

    Only an operator releases it. Reconciliation deliberately does not — the
    registry has no opinion on mechanism, which is the entire reason the floor is
    a column of its own.

    Shape validation is the API schema's job (``validate_optional_schedule_config``,
    shared with the Domain boundary — #254 CR-10/16) so a bad interval is a 422
    rather than a 500.
    """
    watched_item.default_schedule_config = config
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


class ArchivedItemActivationError(Exception):
    """is_active may not change while archived — archive/restore owns activation."""


class SuspendedDomainResumeError(Exception):
    """An item cannot resume while its domain is suspended (kill-switch parity)."""


class RegistryOwnedActivationError(Exception):
    """is_active on a reconciled item is Archiver's — pause/resume there (#254)."""


def set_watched_item_active(
    session: AsyncSession, wi: WatchedItem, *, active: bool, source: str
) -> bool:
    """Apply a pause/resume transition to ``wi``, enforcing the shared guards.

    Returns True when the value changed (an audit event was emitted), False on
    a no-op. Raises :class:`ArchivedItemActivationError` for any attempt while
    archived (even a no-op — restore owns activation),
    :class:`RegistryOwnedActivationError` for any attempt on a reconciled item,
    and :class:`SuspendedDomainResumeError` when resuming while
    ``domain_suspended``. Does not commit; the caller owns the transaction.

    **The registry guard (#254, break-glass ruling).** Once an item has applied
    an announcement (``applied_generation`` set), ``active`` has exactly one
    owner: Archiver's dashboard, over ``info.registry``. A local toggle would
    silently revert within the snapshot period — level-triggered reconciliation
    working as designed — so the 409 names the authority instead of offering a
    control that lies. Raised even on a no-op, matching the archived guard: the
    refusal is what teaches where the control lives. Never-announced rows
    (``applied_generation IS NULL``) keep the local toggle — the registry has no
    opinion to defer to, and until archiver#141's producer announced them they
    had no other control surface. The reconcile itself writes the column
    directly and never passes through here; it is the authority's path, and the
    legitimate local stops remain backoff and ``domain_suspended`` (the
    throttle floor was one until F2, #375).
    """
    if wi.archived_at is not None:
        raise ArchivedItemActivationError(
            "WatchedItem is archived; activation is controlled by restore"
        )
    if wi.applied_generation is not None:
        raise RegistryOwnedActivationError(
            "WatchedItem is registry-owned; pause or resume it in Archiver instead. "
            "A local toggle would be reverted by the next info.registry announcement."
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
