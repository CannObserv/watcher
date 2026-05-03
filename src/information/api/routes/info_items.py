"""InfoItem CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session
from src.information.api.schemas.info_item import InfoItemCreate, InfoItemOut
from src.information.core.models import InfoItem

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


@router.post("", response_model=InfoItemOut, status_code=201)
async def create_info_item(
    body: InfoItemCreate, session: AsyncSession = Depends(get_db_session)
) -> InfoItemOut:
    item = InfoItem(name=body.name, description=body.description, owner=body.owner)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_out(item)


@router.get("", response_model=list[InfoItemOut])
async def list_info_items(
    session: AsyncSession = Depends(get_db_session),
) -> list[InfoItemOut]:
    result = await session.execute(select(InfoItem).order_by(InfoItem.created_at))
    return [_to_out(item) for item in result.scalars().all()]


@router.get("/{info_item_id}", response_model=InfoItemOut)
async def get_info_item(
    info_item_id: str, session: AsyncSession = Depends(get_db_session)
) -> InfoItemOut:
    result = await session.execute(select(InfoItem).where(InfoItem.info_item_id == info_item_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="InfoItem not found")
    return _to_out(item)
