"""Host-level suspension: the one writer of ``Domain.is_active`` (#372).

Deactivating a domain suspends every WatchedItem on it — the denormalized
``WatchedItem.domain_suspended`` that ``schedule_tick`` filters on and
``check_watched_item`` re-checks — and reactivating clears it. The dashboard
toggle and the break-glass CLI (``src.ops.suspend_domain``) both delegate here,
so the CLI imports nothing from ``src.dashboard`` (#377 deletes it) and the two
cannot drift.

Deliberately not in ``src.core.domains``: that module imports
``src.core.fetch_policy``, and with it the bus client and a publisher. The
break-glass exists to work when Archiver is down, so its import path reaches
the database and nothing else (#262). Since F1 (#374) Watcher publishes nothing
on ``content.fetch-policy``, so a suspension acts on Watcher's scheduling alone.
"""

from typing import NamedTuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.watched_item import WatchedItem


class DomainNotFound(LookupError):
    """No ``Domain`` row has this name."""


class DomainArchived(ValueError):
    """The domain is archived; ``is_active`` is not toggled under an archive.

    Archiving makes ``Domain.is_suspended`` true but back-fills no
    ``domain_suspended``, so the items already on it are
    still scheduled. To stop them: restore, then deactivate.
    """


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

    Locks the domain row (``FOR UPDATE``) so a dashboard click and the CLI
    cannot interleave. Raises :class:`DomainNotFound` for an unknown name and
    :class:`DomainArchived` for an archived domain, writing nothing. Audits
    ``domain.activated`` / ``domain.deactivated`` with ``source`` only when
    ``is_active`` actually moves; the back-fill runs either way, so "already
    suspended" is true of the items as well as the row. Does not commit.
    """
    domain = (
        await session.execute(select(Domain).where(Domain.name == name).with_for_update())
    ).scalar_one_or_none()
    if domain is None:
        raise DomainNotFound(name)
    if domain.archived_at is not None:
        raise DomainArchived(name)

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
