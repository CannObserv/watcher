"""Domain rate limiter config API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.schemas.domain import DomainPatch, DomainResponse
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watch import Watch

router = APIRouter(prefix="/api/domains", tags=["domains"])


async def _get_domain_or_404(name: str, session: AsyncSession) -> Domain:
    """Fetch a domain by name, raising 404 if not found."""
    stmt = select(Domain).where(Domain.name == name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return domain


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

    On create: min_interval defaults to 1.0, current_interval defaults to min_interval.
    On update: only provided fields are changed.
    """
    stmt = select(Domain).where(Domain.name == name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()

    updates = data.model_dump(exclude_unset=True)

    if domain is None:
        min_iv = updates.get("min_interval", DEFAULT_MIN_INTERVAL)
        domain = Domain(
            name=name,
            min_interval=min_iv,
            max_concurrency=updates.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            current_interval=min_iv,
        )
        session.add(domain)
    else:
        if "min_interval" in updates:
            domain.min_interval = updates["min_interval"]
        if "max_concurrency" in updates:
            domain.max_concurrency = updates["max_concurrency"]

    await session.commit()
    await session.refresh(domain)
    return domain


@router.delete("/{name}", status_code=204)
async def delete_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Delete a domain config.

    Returns 409 if any watches still reference this domain as their effective_domain.
    """
    domain = await _get_domain_or_404(name, session)

    stmt = select(Watch).where(Watch.effective_domain == name).limit(1)
    result = await session.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: watches still reference domain '{name}'",
        )

    await session.delete(domain)
    await session.commit()
