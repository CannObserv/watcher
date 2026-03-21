"""Pydantic schemas for Change, Snapshot, and SnapshotChunk responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.api.schemas.types import ULIDStr


class SnapshotChunkResponse(BaseModel):
    """Response schema for a snapshot chunk."""

    model_config = ConfigDict(from_attributes=True)
    id: ULIDStr
    snapshot_id: ULIDStr
    chunk_index: int
    chunk_type: str
    chunk_label: str
    content_hash: str
    simhash: int
    char_count: int
    excerpt: str


class SnapshotResponse(BaseModel):
    """Response schema for a snapshot."""

    model_config = ConfigDict(from_attributes=True)
    id: ULIDStr
    watch_id: ULIDStr
    content_hash: str
    simhash: int
    storage_path: str
    text_path: str
    storage_backend: str
    chunk_count: int
    text_bytes: int
    fetch_duration_ms: int
    fetcher_used: str
    fetched_at: datetime


class ChangeResponse(BaseModel):
    """Response schema for a detected change."""

    model_config = ConfigDict(from_attributes=True)
    id: ULIDStr
    watch_id: ULIDStr
    previous_snapshot_id: ULIDStr
    current_snapshot_id: ULIDStr
    change_metadata: dict
    detected_at: datetime
