"""Temporal profile CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.routes.helpers import get_watch_or_404, parse_ulid
from src.api.schemas.temporal_profile import ProfileCreate, ProfileResponse, ProfileUpdate
from src.core.models.audit_log import AuditLog
from src.core.models.temporal_profile import TemporalProfile

router = APIRouter(prefix="/api/watches/{watch_id}/profiles", tags=["temporal-profiles"])


@router.post("", status_code=201, response_model=ProfileResponse)
async def create_profile(
    watch_id: str,
    data: ProfileCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a temporal profile for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    profile = TemporalProfile(
        watch_id=watch.id,
        profile_type=data.profile_type,
        reference_date=data.reference_date,
        date_range_start=data.date_range_start,
        date_range_end=data.date_range_end,
        rules=[r.model_dump() for r in data.rules],
        post_action=data.post_action,
    )
    session.add(profile)
    audit = AuditLog(
        event_type="profile.created",
        watch_id=watch.id,
        payload={"profile_id": str(profile.id), "profile_type": data.profile_type.value},
    )
    session.add(audit)
    await session.commit()
    await session.refresh(profile)
    return profile


@router.get("", response_model=list[ProfileResponse])
async def list_profiles(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """List temporal profiles for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    stmt = (
        select(TemporalProfile)
        .where(TemporalProfile.watch_id == watch.id)
        .order_by(TemporalProfile.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@router.patch("/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    watch_id: str,
    profile_id: str,
    data: ProfileUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Partially update a temporal profile."""
    watch = await get_watch_or_404(watch_id, session)
    profile = await session.get(TemporalProfile, parse_ulid(profile_id, "Profile"))
    if not profile or profile.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Profile not found")
    updates = data.model_dump(exclude_unset=True)
    if "rules" in updates:
        updates["rules"] = [r.model_dump() for r in data.rules]
    for field, value in updates.items():
        setattr(profile, field, value)
    if updates:
        audit = AuditLog(
            event_type="profile.updated",
            watch_id=watch.id,
            payload={"profile_id": str(profile.id), "updated_fields": list(updates.keys())},
        )
        session.add(audit)
    await session.commit()
    await session.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    watch_id: str,
    profile_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a temporal profile."""
    watch = await get_watch_or_404(watch_id, session)
    profile = await session.get(TemporalProfile, parse_ulid(profile_id, "Profile"))
    if not profile or profile.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Profile not found")
    audit = AuditLog(
        event_type="profile.deleted",
        watch_id=watch.id,
        payload={"profile_id": str(profile.id)},
    )
    session.add(audit)
    await session.delete(profile)
    await session.commit()
