"""InfoSpec CRUD endpoints (nested under InfoItem)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session
from src.information.api.schemas.info_spec import (
    InfoSpecCreate,
    InfoSpecOut,
)
from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec,
)
from src.information.core.models import InfoItem, InfoSpec

router = APIRouter(prefix="/info-items/{info_item_id}", tags=["info-specs"])


def _to_out(spec: InfoSpec) -> InfoSpecOut:
    return InfoSpecOut(
        info_spec_id=str(spec.info_spec_id),
        info_item_id=str(spec.info_item_id),
        schema_version=spec.schema_version,
        document=spec.document,
        priority=spec.priority,
        active=spec.active,
        created_at=spec.created_at,
    )


async def _ensure_item_exists(session: AsyncSession, info_item_id: str) -> InfoItem:
    result = await session.execute(select(InfoItem).where(InfoItem.info_item_id == info_item_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="InfoItem not found")
    return item


@router.post("/info-specs", response_model=InfoSpecOut, status_code=201)
async def create_info_spec(
    info_item_id: str,
    body: InfoSpecCreate,
    session: AsyncSession = Depends(get_db_session),
) -> InfoSpecOut:
    await _ensure_item_exists(session, info_item_id)

    try:
        validate_info_spec(body.document)
    except InfoSpecValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    schema_version = body.document["schema_version"]

    # Determine target priority
    if body.priority is None:
        max_p = await session.scalar(
            select(func.coalesce(func.max(InfoSpec.priority), 0)).where(
                InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True)
            )
        )
        target_priority = max_p + 1
    else:
        target_priority = body.priority
        # Shift active rows at >= target_priority by +1 to make room.
        # IMPORTANT: shift in DESCENDING priority order so each UPDATE moves a
        # row into a slot that was just vacated by the previous UPDATE.
        # Ascending order would transiently violate the partial unique index
        # `(info_item_id, priority) WHERE active`.
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
    return _to_out(spec)


@router.get("/info-specs", response_model=list[InfoSpecOut])
async def list_info_specs(
    info_item_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[InfoSpecOut]:
    await _ensure_item_exists(session, info_item_id)
    result = await session.execute(
        select(InfoSpec)
        .where(InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True))
        .order_by(InfoSpec.priority.asc())
    )
    return [_to_out(s) for s in result.scalars().all()]


@router.get("/primary-info-spec", response_model=InfoSpecOut)
async def get_primary_info_spec(
    info_item_id: str,
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
    return _to_out(spec)
