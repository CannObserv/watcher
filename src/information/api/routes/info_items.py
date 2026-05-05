"""InfoItem CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session
from src.information.api.schemas.info_item import (
    InfoItemCreate,
    InfoItemOut,
    InfoItemWithSpecOut,
)
from src.information.api.schemas.types import ULIDStr
from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec,
)
from src.information.core.models import InfoItem, InfoSpec

router = APIRouter(prefix="/info-items", tags=["info-items"])


def _to_out(item: InfoItem) -> InfoItemOut:
    return InfoItemOut(
        info_item_id=str(item.info_item_id),
        name=item.name,
        description=item.description,
        owner=item.owner,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.post("", response_model=InfoItemWithSpecOut, status_code=201)
async def create_info_item(
    body: InfoItemCreate, session: AsyncSession = Depends(get_db_session)
) -> InfoItemWithSpecOut:
    """Create an InfoItem.

    When ``initial_info_spec`` is supplied, validate it first; on success,
    create both the InfoItem and a primary (priority=1, active=True) InfoSpec
    in a single transaction. On validation failure, neither row is written.
    """
    if body.initial_info_spec is not None:
        try:
            validate_info_spec(body.initial_info_spec)
        except InfoSpecValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    item = InfoItem(name=body.name, description=body.description, owner=body.owner)
    session.add(item)
    await session.flush()  # populate item.info_item_id

    info_spec_id: str | None = None
    if body.initial_info_spec is not None:
        spec = InfoSpec(
            info_item_id=item.info_item_id,
            schema_version=body.initial_info_spec["schema_version"],
            document=body.initial_info_spec,
            priority=1,
            active=True,
        )
        session.add(spec)
        await session.flush()
        info_spec_id = str(spec.info_spec_id)

    await session.commit()
    await session.refresh(item)
    base = _to_out(item)
    return InfoItemWithSpecOut(**base.model_dump(), info_spec_id=info_spec_id)


@router.get("", response_model=list[InfoItemOut])
async def list_info_items(
    session: AsyncSession = Depends(get_db_session),
) -> list[InfoItemOut]:
    result = await session.execute(select(InfoItem).order_by(InfoItem.created_at))
    return [_to_out(item) for item in result.scalars().all()]


@router.get("/{info_item_id}", response_model=InfoItemOut)
async def get_info_item(
    info_item_id: ULIDStr, session: AsyncSession = Depends(get_db_session)
) -> InfoItemOut:
    result = await session.execute(select(InfoItem).where(InfoItem.info_item_id == info_item_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="InfoItem not found")
    return _to_out(item)
