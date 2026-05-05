"""InfoSpec CRUD endpoints (nested under InfoItem)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session
from src.information.api.schemas.info_spec import (
    InfoSpecCreate,
    InfoSpecOut,
    InfoSpecPatch,
)
from src.information.api.schemas.types import ULIDStr
from src.information.api.serializers import info_spec_to_out
from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec,
)
from src.information.core.models import InfoItem, InfoSpec

router = APIRouter(prefix="/info-items/{info_item_id}", tags=["info-specs"])


async def _ensure_item_exists(session: AsyncSession, info_item_id: str) -> None:
    result = await session.execute(select(InfoItem).where(InfoItem.info_item_id == info_item_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="InfoItem not found")


async def _shift_active_priorities_at_or_above(
    session: AsyncSession, info_item_id: str, target_priority: int
) -> None:
    """Shift all active rows with priority >= target_priority by +1.

    Required to make room for a new/moved spec at target_priority.
    Iterates in DESCENDING priority order so each UPDATE moves a row into a
    slot just vacated by the previous UPDATE; ascending order would
    transiently violate the partial unique index `(info_item_id, priority)
    WHERE active`.
    """
    rows = (
        (
            await session.execute(
                select(InfoSpec)
                .where(
                    InfoSpec.info_item_id == info_item_id,
                    InfoSpec.active.is_(True),
                    InfoSpec.priority >= target_priority,
                )
                .order_by(InfoSpec.priority.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.priority = row.priority + 1
    await session.flush()


@router.post("/info-specs", response_model=InfoSpecOut, status_code=201)
async def create_info_spec(
    info_item_id: ULIDStr,
    body: InfoSpecCreate,
    session: AsyncSession = Depends(get_db_session),
) -> InfoSpecOut:
    await _ensure_item_exists(session, info_item_id)

    try:
        validate_info_spec(body.document)
    except InfoSpecValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    schema_version = body.document["schema_version"]

    if body.priority is None:
        max_p = await session.scalar(
            select(func.coalesce(func.max(InfoSpec.priority), 0)).where(
                InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True)
            )
        )
        target_priority = max_p + 1
    else:
        target_priority = body.priority
        await _shift_active_priorities_at_or_above(session, info_item_id, target_priority)

    spec = InfoSpec(
        info_item_id=info_item_id,
        schema_version=schema_version,
        document=body.document,
        priority=target_priority,
        active=True,
    )
    session.add(spec)
    await session.commit()
    await session.refresh(spec)
    return info_spec_to_out(spec)


@router.get("/info-specs", response_model=list[InfoSpecOut])
async def list_info_specs(
    info_item_id: ULIDStr,
    session: AsyncSession = Depends(get_db_session),
) -> list[InfoSpecOut]:
    await _ensure_item_exists(session, info_item_id)
    result = await session.execute(
        select(InfoSpec)
        .where(InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True))
        .order_by(InfoSpec.priority.asc())
    )
    return [info_spec_to_out(s) for s in result.scalars().all()]


@router.get("/primary-info-spec", response_model=InfoSpecOut)
async def get_primary_info_spec(
    info_item_id: ULIDStr,
    session: AsyncSession = Depends(get_db_session),
) -> InfoSpecOut:
    """Return the lowest-priority active InfoSpec for the InfoItem.

    Hot path for consumer services (Watcher, Archive).
    """
    await _ensure_item_exists(session, info_item_id)
    result = await session.execute(
        select(InfoSpec)
        .where(InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True))
        .order_by(InfoSpec.priority.asc())
        .limit(1)
    )
    spec = result.scalar_one_or_none()
    if spec is None:
        raise HTTPException(status_code=404, detail="No active InfoSpec for InfoItem")
    return info_spec_to_out(spec)


@router.patch("/info-specs/{info_spec_id}", response_model=InfoSpecOut)
async def patch_info_spec(
    info_item_id: ULIDStr,
    info_spec_id: ULIDStr,
    body: InfoSpecPatch,
    session: AsyncSession = Depends(get_db_session),
) -> InfoSpecOut:
    """Mutate placement metadata (priority, active). Document body is immutable."""
    await _ensure_item_exists(session, info_item_id)

    result = await session.execute(
        select(InfoSpec).where(
            InfoSpec.info_spec_id == info_spec_id,
            InfoSpec.info_item_id == info_item_id,
        )
    )
    spec = result.scalar_one_or_none()
    if spec is None:
        raise HTTPException(status_code=404, detail="InfoSpec not found")

    target_active = body.active if body.active is not None else spec.active
    target_priority = body.priority

    if target_active and target_priority is None and not spec.active:
        # Reactivating without explicit priority: append at end.
        max_p = await session.scalar(
            select(func.coalesce(func.max(InfoSpec.priority), 0)).where(
                InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True)
            )
        )
        target_priority = max_p + 1

    # Fire shift whenever the spec is going to be active at a target_priority
    # AND either (a) the priority is changing, or (b) the spec is transitioning
    # from inactive to active. Case (b) is critical: another active spec may now
    # occupy this priority value, so the shift block must run even when
    # `target_priority == spec.priority`.
    needs_shift = (
        target_active
        and target_priority is not None
        and (target_priority != spec.priority or not spec.active)
    )
    if needs_shift:
        # Park the spec at priority=0 so its current slot doesn't block the
        # shift of other rows (the partial unique index covers active rows only,
        # but two active rows can't share a priority value even transiently).
        spec.priority = 0
        await session.flush()
        await _shift_active_priorities_at_or_above(session, info_item_id, target_priority)

    spec.active = target_active
    if target_priority is not None:
        spec.priority = target_priority

    await session.commit()
    await session.refresh(spec)
    return info_spec_to_out(spec)
