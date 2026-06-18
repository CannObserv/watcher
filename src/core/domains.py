"""Domain resolution helpers shared by the WatchedItem create/patch paths.

These mirror the create-time probe path's domain handling but take a URL or
hostname that is already known (Archiver is authoritative for ``effective_url``),
so no network probe is performed. Centralising the upsert + suspension logic
keeps the API create branch, the API PATCH branch, and the dashboard re-probe
route from drifting (#196).
"""

from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain


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
    """Upsert the Domain row for ``domain_name`` and return its suspension state.

    Idempotent and ``IntegrityError``-safe (mirrors the create-time upsert). The
    returned bool is True when the domain exists and is archived or inactive, so
    callers can set ``WatchedItem.domain_suspended`` without a live Domain join.
    A freshly-created domain (or an empty/None ``domain_name``) resolves to False.
    """
    if not domain_name:
        return False
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
        return bool(existing.archived_at is not None or not existing.is_active)
    return False
