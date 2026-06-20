"""Domain resolution helpers shared by the WatchedItem create/patch paths.

These mirror the create-time probe path's domain handling but take a URL or
hostname that is already known (Archiver is authoritative for ``effective_url``),
so no network probe is performed. Centralising the upsert + suspension logic
keeps the API create branch, the API PATCH branch, and the dashboard re-probe
route from drifting (#196).
"""

from typing import NamedTuple
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain


class DomainResolution(NamedTuple):
    """Domain facts denormalized onto a WatchedItem at create/PATCH time (#196, #205).

    ``suspended`` gates scheduling (domain archived or inactive);
    ``default_schedule_config`` is the domain's cadence tier, copied to
    ``WatchedItem.domain_default_schedule_config`` so the resolver needs no live
    Domain join. Both are re-evaluated together on every create/PATCH path.
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
    domain's cadence tier (#205). Callers copy both onto the WatchedItem so the
    scheduler needs no live Domain join. A freshly-created domain (or an
    empty/None ``domain_name``) resolves to ``(False, None)``.
    """
    if not domain_name:
        return DomainResolution(suspended=False, default_schedule_config=None)
    existing = (
        await session.execute(select(Domain).where(Domain.name == domain_name))
    ).scalar_one_or_none()
    if existing is None:
        try:
            async with session.begin_nested():
                session.add(
                    Domain(
                        name=domain_name,
                        min_interval=DEFAULT_MIN_INTERVAL,
                        max_concurrency=DEFAULT_MAX_CONCURRENCY,
                        current_interval=DEFAULT_MIN_INTERVAL,
                    )
                )
        except IntegrityError:
            existing = (
                await session.execute(select(Domain).where(Domain.name == domain_name))
            ).scalar_one_or_none()
    if existing is not None:
        return DomainResolution(
            suspended=bool(existing.archived_at is not None or not existing.is_active),
            default_schedule_config=existing.default_schedule_config,
        )
    return DomainResolution(suspended=False, default_schedule_config=None)
