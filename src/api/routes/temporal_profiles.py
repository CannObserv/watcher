"""Temporal profile CRUD API endpoints (#191: one profile per WatchedItem)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.helpers import get_watched_item_or_404, parse_ulid
from src.api.schemas.temporal_profile import ProfileCreate, ProfileResponse, ProfileUpdate
from src.core.models.audit_log import EventType, audit
from src.core.models.temporal_profile import TemporalProfile

router = APIRouter(prefix="/watched-items/{watched_item_id}/profiles", tags=["temporal-profiles"])


@router.post("", status_code=201, response_model=ProfileResponse)
async def create_profile(
    watched_item_id: str,
    data: ProfileCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create the temporal profile for a WatchedItem (one per item)."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    existing = await session.scalar(
        select(TemporalProfile).where(TemporalProfile.watched_item_id == wi.id)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="WatchedItem already has a temporal profile")
    profile = TemporalProfile(
        watched_item_id=wi.id,
        profile_type=data.profile_type,
        reference_date=data.reference_date,
        date_range_start=data.date_range_start,
        date_range_end=data.date_range_end,
        rules=[r.model_dump() for r in data.rules],
        post_action=data.post_action,
    )
    session.add(profile)
    audit(
        session,
        EventType.PROFILE_CREATED,
        watched_item_id=str(wi.id),
        profile_id=str(profile.id),
        profile_type=data.profile_type.value,
    )
    await session.commit()
    await session.refresh(profile)
    return profile


@router.get("", response_model=list[ProfileResponse])
async def list_profiles(
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """List the WatchedItem's temporal profile (zero or one)."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    stmt = (
        select(TemporalProfile)
        .where(TemporalProfile.watched_item_id == wi.id)
        .order_by(TemporalProfile.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@router.patch("/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    watched_item_id: str,
    profile_id: str,
    data: ProfileUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Partially update a temporal profile."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    profile = await session.get(TemporalProfile, parse_ulid(profile_id, "Profile"))
    if not profile or profile.watched_item_id != wi.id:
        raise HTTPException(status_code=404, detail="Profile not found")
    updates = data.model_dump(exclude_unset=True)
    if "rules" in updates:
        updates["rules"] = [r.model_dump() for r in data.rules]
    for field, value in updates.items():
        setattr(profile, field, value)
    if updates:
        audit(
            session,
            EventType.PROFILE_UPDATED,
            watched_item_id=str(wi.id),
            profile_id=str(profile.id),
            updated_fields=list(updates.keys()),
        )
    await session.commit()
    await session.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    watched_item_id: str,
    profile_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a temporal profile."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    profile = await session.get(TemporalProfile, parse_ulid(profile_id, "Profile"))
    if not profile or profile.watched_item_id != wi.id:
        raise HTTPException(status_code=404, detail="Profile not found")
    audit(
        session,
        EventType.PROFILE_DELETED,
        watched_item_id=str(wi.id),
        profile_id=str(profile.id),
    )
    await session.delete(profile)
    await session.commit()
