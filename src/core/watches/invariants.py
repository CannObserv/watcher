"""Lifecycle invariants for root vs. fragment Watches.

A "chain" is the parent_info_source_id sequence from a fragment up to its
URL-bearing root. Fragment Watches require an active root Watch somewhere
on the chain; root Watches block deletion when fragment Watches depend on
them.
"""

from archiver_client import ArchiverClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.watch import Watch


class RootWatchMissingError(Exception):
    """Fragment Watch creation without an active root Watch."""


class FragmentDependentsExistError(Exception):
    """Root Watch delete attempted while fragment Watches depend on it."""


async def _walk_to_root(client: ArchiverClient, info_source_id: str) -> list[str]:
    """Chain of info_source_ids from leaf → root (inclusive)."""
    chain = []
    current_id = info_source_id
    while current_id is not None:
        chain.append(current_id)
        source = await client.get_info_source(current_id)
        parent = source.parent_info_source_id
        current_id = str(parent) if parent is not None else None
    return chain


async def require_root_watch_on_chain(
    session: AsyncSession,
    client: ArchiverClient,
    *,
    info_source_id: str,
) -> None:
    """No-op if an active Watch exists on the chain; raise otherwise."""
    chain = await _walk_to_root(client, info_source_id)
    result = await session.execute(
        select(Watch.id)
        .where(Watch.info_source_id.in_(chain))
        .where(Watch.is_active.is_(True))
        .where(Watch.is_archived.is_(False))
    )
    if result.scalar_one_or_none() is None:
        raise RootWatchMissingError(
            f"no active Watch on chain rooted at {chain[-1]} (target {info_source_id})"
        )


async def require_no_fragment_dependents(
    session: AsyncSession,
    client: ArchiverClient,
    root_watch: Watch,
) -> None:
    """Refuse to delete a root Watch whose source has fragment Watches."""
    page = await client.list_info_sources(parent_info_source_id=str(root_watch.info_source_id))
    fragment_ids = [str(f.info_source_id) for f in page.items]
    if not fragment_ids:
        return
    result = await session.execute(
        select(Watch.id, Watch.info_source_id)
        .where(Watch.info_source_id.in_(fragment_ids))
        .where(Watch.is_archived.is_(False))
    )
    dependents = [(str(wid), str(sid)) for wid, sid in result.all()]
    if dependents:
        raise FragmentDependentsExistError(f"root Watch has fragment dependents: {dependents}")
