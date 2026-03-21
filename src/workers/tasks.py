"""Check-watch pipeline — core orchestration for fetching, extracting, diffing."""

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.differ import ChangeStatus, ChunkFingerprint, diff_chunks
from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.extractors.base import ExtractionResult
from src.core.logging import get_logger
from src.core.models.audit_log import AuditLog
from src.core.models.base import generate_ulid
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch
from src.core.simhash import simhash
from src.core.storage import StorageBackend

logger = get_logger(__name__)

_INT64_MAX = (1 << 63) - 1


def _to_signed64(val: int) -> int:
    """Convert unsigned 64-bit simhash to signed int64 for PostgreSQL BIGINT."""
    if val > _INT64_MAX:
        return val - (1 << 64)
    return val


_EXT_MAP = {
    ContentType.HTML: "html",
    ContentType.PDF: "pdf",
    ContentType.FILE: "csv",
}

_EXTRACTOR_MAP = {
    ContentType.HTML: HtmlExtractor,
    ContentType.PDF: PdfExtractor,
    ContentType.FILE: CsvExcelExtractor,
}


async def _get_previous_snapshot(
    session: AsyncSession, watch_id: object,
) -> Snapshot | None:
    """Fetch most recent snapshot for a watch, or None."""
    stmt = (
        select(Snapshot)
        .where(Snapshot.watch_id == watch_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_snapshot_chunks(
    session: AsyncSession, snapshot_id: object,
) -> list[SnapshotChunk]:
    """Fetch all chunks for a snapshot ordered by index."""
    stmt = (
        select(SnapshotChunk)
        .where(SnapshotChunk.snapshot_id == snapshot_id)
        .order_by(SnapshotChunk.chunk_index)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _extract_content(watch: Watch, raw_content: bytes) -> ExtractionResult:
    """Run the appropriate extractor based on watch content_type."""
    extractor_cls = _EXTRACTOR_MAP[watch.content_type]
    extractor = extractor_cls()
    config: dict | None = None
    if watch.content_type == ContentType.FILE:
        config = {"content_type": "csv"}
    return extractor.extract(raw_content, config=config)


async def _run_check_pipeline(
    watch: Watch,
    raw_content: bytes,
    fetcher_used: str,
    fetch_duration_ms: int,
    storage: StorageBackend,
    session: AsyncSession,
) -> dict:
    """Core check pipeline: hash, extract, diff, store.

    Returns dict with snapshot_id, is_changed, change_id, chunk_count, storage_path.
    """
    # 1. Compute content hash and doc-level simhash
    content_hash = hashlib.sha256(raw_content).hexdigest()
    doc_simhash = _to_signed64(simhash(raw_content.decode(errors="replace")))

    # 2. Check previous snapshot
    prev_snapshot = await _get_previous_snapshot(session, watch.id)

    # 3. Fast path: identical content
    if prev_snapshot and prev_snapshot.content_hash == content_hash:
        logger.info("no change detected", extra={"watch_id": str(watch.id)})
        session.add(AuditLog(
            event_type="check.no_change",
            watch_id=watch.id,
            payload={"content_hash": content_hash},
        ))
        await session.flush()
        return {
            "snapshot_id": None,
            "is_changed": False,
            "change_id": None,
            "chunk_count": 0,
            "storage_path": None,
        }

    # 4. Extract content
    extraction = _extract_content(watch, raw_content)

    # 5. Store raw + extracted text
    snapshot_id = generate_ulid()
    ext = _EXT_MAP[watch.content_type]
    raw_path = storage.snapshot_path(str(watch.id), str(snapshot_id), ext)
    text_path = storage.snapshot_path(str(watch.id), str(snapshot_id), "txt")
    storage.save(raw_path, raw_content)
    full_text = "\n".join(c.text for c in extraction.chunks)
    storage.save(text_path, full_text.encode())

    # 6. Create Snapshot record
    snapshot = Snapshot(
        id=snapshot_id,
        watch_id=watch.id,
        content_hash=content_hash,
        simhash=doc_simhash,
        storage_path=raw_path,
        text_path=text_path,
        storage_backend="local",
        chunk_count=len(extraction.chunks),
        text_bytes=len(full_text.encode()),
        fetch_duration_ms=fetch_duration_ms,
        fetcher_used=fetcher_used,
    )
    session.add(snapshot)
    await session.flush()

    # 7. Create SnapshotChunk records
    for chunk in extraction.chunks:
        session.add(SnapshotChunk(
            snapshot_id=snapshot_id,
            chunk_index=chunk.index,
            chunk_type=chunk.chunk_type,
            chunk_label=chunk.label,
            content_hash=chunk.content_hash,
            simhash=_to_signed64(chunk.simhash),
            char_count=chunk.char_count,
            excerpt=chunk.excerpt,
        ))
    await session.flush()

    # 8-9. Diff against previous if exists
    change_id = None
    if prev_snapshot:
        prev_chunks_db = await _get_snapshot_chunks(session, prev_snapshot.id)
        prev_fingerprints = [
            ChunkFingerprint(
                index=c.chunk_index, label=c.chunk_label,
                content_hash=c.content_hash, simhash=c.simhash,
            )
            for c in prev_chunks_db
        ]
        curr_fingerprints = [
            ChunkFingerprint(
                index=c.index, label=c.label,
                content_hash=c.content_hash, simhash=c.simhash,
            )
            for c in extraction.chunks
        ]
        changes = diff_chunks(prev_fingerprints, curr_fingerprints)
        has_real_changes = any(
            ch.status in (ChangeStatus.ADDED, ChangeStatus.REMOVED, ChangeStatus.MODIFIED)
            for ch in changes
        )
        if has_real_changes:
            metadata = {
                "added": [c.chunk_label for c in changes if c.status == ChangeStatus.ADDED],
                "removed": [c.chunk_label for c in changes if c.status == ChangeStatus.REMOVED],
                "modified": [
                    {"label": c.chunk_label, "similarity": c.similarity}
                    for c in changes if c.status == ChangeStatus.MODIFIED
                ],
            }
            change = Change(
                watch_id=watch.id,
                previous_snapshot_id=prev_snapshot.id,
                current_snapshot_id=snapshot_id,
                change_metadata=metadata,
            )
            session.add(change)
            await session.flush()
            change_id = change.id

    # 10. Audit log
    session.add(AuditLog(
        event_type="check.snapshot_created",
        watch_id=watch.id,
        payload={
            "snapshot_id": str(snapshot_id),
            "content_hash": content_hash,
            "chunk_count": len(extraction.chunks),
            "is_changed": change_id is not None or prev_snapshot is None,
        },
    ))
    await session.flush()

    # 11. Return result
    return {
        "snapshot_id": str(snapshot_id),
        "is_changed": change_id is not None or prev_snapshot is None,
        "change_id": str(change_id) if change_id else None,
        "chunk_count": len(extraction.chunks),
        "storage_path": raw_path,
    }
