"""Domain state: resolution for the WatchedItem paths, and host suspension.

Resolution mirrors the create-time probe path's domain handling but takes a URL
or hostname that is already known (Archiver is authoritative for
``effective_url``), so no network probe is performed. Centralising the upsert +
suspension-resolution logic keeps the API create branch, the API PATCH branch,
and the dashboard /effective-url route from drifting (#196).

``set_domain_active`` is the one writer of ``Domain.is_active`` (#372).
Deactivating a domain suspends every WatchedItem on it — the denormalized
``WatchedItem.domain_suspended`` that ``schedule_tick`` filters on and
``check_watched_item`` re-checks — and reactivating clears it. The dashboard
toggle and the break-glass CLI (``src.ops.suspend_domain``) both delegate here,
so the CLI imports nothing from ``src.dashboard`` (#377 deletes it) and the two
cannot drift. A suspension acts on Watcher's scheduling alone: since F1 (#374)
nothing downstream sees it.

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
from src.core.models.domain import DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watched_item import WatchedItem


class DomainResolution(NamedTuple):
    """Domain facts denormalized onto a WatchedItem at create/PATCH time (#196, #205).

    ``suspended`` gates scheduling (domain archived or inactive);
    ``default_schedule_config`` is the domain's cadence, copied to
    ``WatchedItem.domain_default_schedule_config``. It was the resolver's Domain
    tier and is inert since F2 (#375); the copy is still maintained until
    #377/#378 delete the writers. Both are re-evaluated together on every
    create/PATCH path.
    """

    suspended: bool
    default_schedule_config: dict | None


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
) -> DomainResolution:
    """Upsert the Domain row for ``domain_name`` and return its denormalizable state.

    Idempotent and ``IntegrityError``-safe (mirrors the create-time upsert).
    Returns a :class:`DomainResolution`: ``suspended`` is True when the domain
    exists and is archived or inactive; ``default_schedule_config`` is the
    domain's cadence (#205; inert since #375). Callers copy both onto the
    WatchedItem so the scheduler needs no live Domain join. A freshly-created
    domain (or an empty/None ``domain_name``) resolves to ``(False, None)``.
    """
    if not domain_name:
        return DomainResolution(suspended=False, default_schedule_config=None)
    existing = (
        await session.execute(select(Domain).where(Domain.name == domain_name))
    ).scalar_one_or_none()
    if existing is None:
        try:
            async with session.begin_nested():
                session.add(Domain(name=domain_name, min_interval=DEFAULT_MIN_INTERVAL))
        except IntegrityError:
            existing = (
                await session.execute(select(Domain).where(Domain.name == domain_name))
            ).scalar_one_or_none()
    if existing is not None:
        return DomainResolution(
            # Domain.is_suspended, not an inline repeat of it: one spelling of
            # the suspension predicate (#250, CR-1 finding 7).
            suspended=existing.is_suspended,
            default_schedule_config=existing.default_schedule_config,
        )
    return DomainResolution(suspended=False, default_schedule_config=None)


async def backfill_domain_schedule_config(
    session: AsyncSession, domain_name: str, config: dict | None
) -> None:
    """Propagate a domain's cadence to every WatchedItem on it (#205).

    Mirrors the ``domain_suspended`` back-fill (``set_domain_active``): the
    denormalized ``WatchedItem.domain_default_schedule_config`` is kept in sync
    when an operator edits the domain cadence. Inert since F2 (#375) — the
    resolver no longer reads the copy — and kept until #377/#378 delete the
    domain cadence writers. One bounded UPDATE on a rare operator action. Does
    not commit.
    """
    await session.execute(
        update(WatchedItem)
        .where(WatchedItem.domain_name == domain_name)
        .values(domain_default_schedule_config=config)
    )


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
