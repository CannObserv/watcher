"""Domain state: resolution for the WatchedItem paths, and host suspension.

Resolution takes a URL or hostname that is already known (Archiver is
authoritative for ``effective_url``), so no network probe is performed.
Its one caller is the registry reconcile, the only create path since the
API's create and PATCH branches went with ``/api/v1`` (#378) and the deferred
probe's resolution went with PROBING (#380).

``set_domain_active`` is the one writer of ``Domain.is_active`` (#372).
Deactivating a domain suspends every WatchedItem on it — the denormalized
``WatchedItem.domain_suspended`` that ``schedule_tick`` filters on and
``check_watched_item`` re-checks — and reactivating clears it. The break-glass
CLI (``src.ops.suspend_domain``) delegates here, the one control left since #377
deleted the dashboard's toggle. A suspension acts on Watcher's scheduling alone:
since F1 (#374) nothing downstream sees it.

**The break-glass imports this module**, and it exists to work when Archiver is
down, so this module imports no bus, no worker, no dashboard and no API
(#262). ``tests/ops/test_suspend_domain.py`` pins that import set.
"""

from typing import NamedTuple
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.watched_item import WatchedItem


def domain_name_for_url(url: str | None) -> str | None:
    """Return the hostname for ``url`` (no network probe), or None when absent.

    Used to derive ``WatchedItem.domain_name`` from an already-resolved
    ``effective_url`` without re-probing.
    """
    if not url:
        return None
    return urlparse(url).hostname or None


async def ensure_domain_and_resolve_suspension(
    session: AsyncSession, domain_name: str | None
) -> bool:
    """Upsert the Domain row for ``domain_name`` and return whether it is suspended.

    Idempotent and ``IntegrityError``-safe (mirrors the create-time upsert).
    True when the domain exists and is deactivated. The caller copies it onto
    ``WatchedItem.domain_suspended`` so the scheduler needs no live Domain
    join (#196). A freshly-created domain, or an empty/None ``domain_name``,
    is not suspended.
    """
    if not domain_name:
        return False
    existing = (
        await session.execute(select(Domain).where(Domain.name == domain_name))
    ).scalar_one_or_none()
    if existing is None:
        try:
            async with session.begin_nested():
                session.add(Domain(name=domain_name))
        except IntegrityError:
            existing = (
                await session.execute(select(Domain).where(Domain.name == domain_name))
            ).scalar_one_or_none()
    # Domain.is_suspended, not an inline repeat of it: one spelling of the
    # suspension predicate (#250, CR-1 finding 7).
    return existing is not None and existing.is_suspended


class DomainNotFound(LookupError):
    """No ``Domain`` row has this name."""


class DomainActivation(NamedTuple):
    """What ``set_domain_active`` did.

    ``changed`` is whether ``Domain.is_active`` moved (and so whether an audit
    row was written); ``items_updated`` counts the WatchedItems whose
    ``domain_suspended`` was rewritten. The two are independent: a no-op still
    re-flags any item that disagrees with its domain.
    """

    domain: Domain
    changed: bool
    items_updated: int


async def set_domain_active(
    session: AsyncSession, name: str, *, active: bool, source: str
) -> DomainActivation:
    """Activate or deactivate domain ``name`` and back-fill its items.

    Locks the domain row (``FOR UPDATE``) so two concurrent writers cannot
    interleave. Raises :class:`DomainNotFound` for an unknown name, writing
    nothing. Audits
    ``domain.activated`` / ``domain.deactivated`` with ``source`` only when
    ``is_active`` actually moves; the back-fill runs either way, so "already
    suspended" is true of the items as well as the row. Does not commit.
    """
    domain = (
        await session.execute(select(Domain).where(Domain.name == name).with_for_update())
    ).scalar_one_or_none()
    if domain is None:
        raise DomainNotFound(name)

    changed = domain.is_active != active
    domain.is_active = active

    result = await session.execute(
        update(WatchedItem)
        .where(
            WatchedItem.domain_name == name,
            WatchedItem.domain_suspended.is_not(not active),
        )
        .values(domain_suspended=not active)
    )
    if changed:
        audit(
            session,
            EventType.DOMAIN_ACTIVATED if active else EventType.DOMAIN_DEACTIVATED,
            domain_name=name,
            source=source,
        )
    return DomainActivation(domain=domain, changed=changed, items_updated=result.rowcount)
