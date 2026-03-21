"""Changes API endpoints — list and detail with embedded snapshots."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.change import ChangeResponse, SnapshotChunkResponse, SnapshotResponse
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk

router = APIRouter(prefix="/api/changes", tags=["changes"])


class SnapshotWithChunksResponse(SnapshotResponse):
    """Snapshot response with embedded chunks."""

    chunks: list[SnapshotChunkResponse] = []


class ChangeDetailResponse(ChangeResponse):
    """Change response with embedded current and previous snapshots."""

    current_snapshot: SnapshotWithChunksResponse | None
    previous_snapshot: SnapshotWithChunksResponse | None


def _parse_ulid(value: str) -> ULID:
    """Parse a ULID string, raising 404 on invalid format."""
    try:
        return ULID.from_str(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Change not found") from exc


@router.get("", response_model=list[ChangeResponse])
async def list_changes(
    watch_id: str | None = Query(None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    """List changes with optional watch_id filter and pagination."""
    stmt = select(Change).order_by(Change.detected_at.desc())
    if watch_id is not None:
        stmt = stmt.where(Change.watch_id == _parse_ulid(watch_id))
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


async def _load_snapshot_with_chunks(
    session: AsyncSession, snapshot_id: ULID
) -> dict | None:
    """Load a snapshot and its chunks, returning a dict for response construction."""
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None:
        return None
    chunk_stmt = (
        select(SnapshotChunk)
        .where(SnapshotChunk.snapshot_id == snapshot_id)
        .order_by(SnapshotChunk.chunk_index)
    )
    chunk_result = await session.execute(chunk_stmt)
    chunks = chunk_result.scalars().all()

    snap_dict = SnapshotResponse.model_validate(snapshot).model_dump()
    snap_dict["chunks"] = [
        SnapshotChunkResponse.model_validate(c).model_dump() for c in chunks
    ]
    return snap_dict


@router.get("/{change_id}", response_model=ChangeDetailResponse)
async def get_change_detail(
    change_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Get change detail with embedded snapshots and their chunks."""
    change = await session.get(Change, _parse_ulid(change_id))
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    current = await _load_snapshot_with_chunks(session, change.current_snapshot_id)
    previous = await _load_snapshot_with_chunks(session, change.previous_snapshot_id)

    change_dict = ChangeResponse.model_validate(change).model_dump()
    change_dict["current_snapshot"] = current
    change_dict["previous_snapshot"] = previous
    return ChangeDetailResponse.model_validate(change_dict)
