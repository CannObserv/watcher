"""Domain config API endpoints — politeness floor, cadence, lifecycle."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.schemas.domain import DomainPatch, DomainResponse
from src.core.domains import backfill_domain_schedule_config
from src.core.fetch_policy import clear_tombstone, record_tombstone
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watched_item import WatchedItem
from src.workers.fetch_policy import defer_policy_republish

router = APIRouter(prefix="/domains", tags=["domains"])


async def _get_domain_or_404(name: str, session: AsyncSession) -> Domain:
    """Fetch a domain by name, raising 404 if not found."""
    stmt = select(Domain).where(Domain.name == name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return domain


def _apply_domain_updates(domain: Domain, updates: dict) -> None:
    """Apply provided ``DomainPatch`` fields onto an existing Domain row."""
    if "min_interval" in updates:
        domain.min_interval = updates["min_interval"]
    if "notes" in updates:
        domain.notes = updates["notes"]
    if "default_schedule_config" in updates:
        domain.default_schedule_config = updates["default_schedule_config"]


@router.get("", response_model=list[DomainResponse])
async def list_domains(session: AsyncSession = Depends(get_db_session)):
    """List all domain configs."""
    result = await session.execute(select(Domain).order_by(Domain.name))
    return result.scalars().all()


@router.get("/{name}", response_model=DomainResponse)
async def get_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Get a domain config by hostname."""
    return await _get_domain_or_404(name, session)


@router.patch("/{name}", response_model=DomainResponse)
async def upsert_domain(
    name: str,
    data: DomainPatch,
    session: AsyncSession = Depends(get_db_session),
):
    """Create or update a domain config (upsert by hostname).

    On create: min_interval defaults to 1.0.
    On update: only provided fields are changed.
    """
    stmt = select(Domain).where(Domain.name == name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()

    updates = data.model_dump(exclude_unset=True)

    if domain is None:
        # The host is live again: its fetch-policy tombstone (if any) must stop
        # being republished, atomically with the row that supersedes it (#245).
        await clear_tombstone(session, name)
        domain = Domain(
            name=name,
            min_interval=updates.get("min_interval", DEFAULT_MIN_INTERVAL),
            notes=updates.get("notes"),
            default_schedule_config=updates.get("default_schedule_config"),
        )
        session.add(domain)
        try:
            await session.flush()
        except IntegrityError:
            # Concurrent request created the domain between our select and insert.
            # The constructor's field values were rolled back, so re-apply the
            # provided fields onto the winning row — otherwise this PATCH would
            # silently no-op against the concurrent default.
            await session.rollback()
            result = await session.execute(select(Domain).where(Domain.name == name))
            domain = result.scalar_one()
            _apply_domain_updates(domain, updates)
    else:
        _apply_domain_updates(domain, updates)

    # #205: editing the domain cadence re-denormalizes it onto every WatchedItem
    # on the domain (mirrors the domain_suspended back-fill), so the resolver
    # never needs a live Domain join. Rare operator action; one bounded UPDATE.
    if "default_schedule_config" in updates:
        await backfill_domain_schedule_config(session, name, updates["default_schedule_config"])
        audit(
            session,
            EventType.DOMAIN_UPDATED,
            domain_name=name,
            default_schedule_config=updates["default_schedule_config"],
            source="api",
        )

    await session.commit()
    await session.refresh(domain)
    # Post-commit so the republished set reads the new numbers; best-effort —
    # the periodic tick covers a failed defer (#245).
    await defer_policy_republish()
    return domain


@router.delete("/{name}", status_code=204)
async def delete_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Delete a domain config.

    Returns 409 if any watched items still reference this domain.
    """
    domain = await _get_domain_or_404(name, session)

    stmt = select(WatchedItem).where(WatchedItem.domain_name == name).limit(1)
    result = await session.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: watched items still reference domain '{name}'",
        )

    # LWW streams have no delete: the tombstone row keeps the host's revocation
    # in every full-set republish, atomically with the Domain delete (#245).
    await record_tombstone(session, name)
    await session.delete(domain)
    await session.commit()
    await defer_policy_republish()


@router.post("/{name}/archive", response_model=DomainResponse)
async def archive_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Archive a domain — suspends checks for every WatchedItem on it.

    Republishes, because since #250 suspension revokes the host's policy on the
    wire. Without the defer the revocation waits for the five-minute full set,
    and Replicator goes on pacing a host Watcher has already stopped watching.
    """
    domain = await _get_domain_or_404(name, session)
    if domain.archived_at is None:
        domain.archived_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(domain)
    await defer_policy_republish()
    return domain


@router.post("/{name}/restore", response_model=DomainResponse)
async def restore_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Restore an archived domain.

    Republishes for the same reason as archive (#250): the live interval should
    reach Replicator when the operator restores the domain, not up to five
    minutes later.
    """
    domain = await _get_domain_or_404(name, session)
    domain.archived_at = None
    await session.commit()
    await session.refresh(domain)
    await defer_policy_republish()
    return domain
