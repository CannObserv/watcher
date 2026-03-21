"""Chunk-level change detection — compare two sets of chunks by hash."""

import enum
from dataclasses import dataclass

from src.core.simhash import similarity as simhash_similarity


@dataclass
class ChunkFingerprint:
    """Lightweight DTO for chunk comparison — no text, no hash recomputation."""

    index: int
    label: str
    content_hash: str
    simhash: int


class ChangeStatus(enum.StrEnum):
    """Status of a chunk comparison."""

    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    ADDED = "added"
    REMOVED = "removed"


@dataclass
class ChunkChange:
    """Result of comparing a single chunk between snapshots."""

    chunk_index: int
    chunk_label: str
    status: ChangeStatus
    similarity: float | None = None


def diff_chunks(previous: list, current: list) -> list[ChunkChange]:
    """Compare two ordered lists of diffable objects by index and content hash.

    Accepts any objects with index, label, content_hash, and simhash attributes
    (Chunk, ChunkFingerprint, or any duck-typed equivalent).
    """
    prev_by_index = {c.index: c for c in previous}
    curr_by_index = {c.index: c for c in current}

    all_indices = sorted(set(prev_by_index.keys()) | set(curr_by_index.keys()))
    changes = []

    for idx in all_indices:
        prev_chunk = prev_by_index.get(idx)
        curr_chunk = curr_by_index.get(idx)

        if prev_chunk is None and curr_chunk is not None:
            changes.append(ChunkChange(
                chunk_index=idx,
                chunk_label=curr_chunk.label,
                status=ChangeStatus.ADDED,
            ))
        elif prev_chunk is not None and curr_chunk is None:
            changes.append(ChunkChange(
                chunk_index=idx,
                chunk_label=prev_chunk.label,
                status=ChangeStatus.REMOVED,
            ))
        elif prev_chunk is not None and curr_chunk is not None:
            if prev_chunk.content_hash == curr_chunk.content_hash:
                changes.append(ChunkChange(
                    chunk_index=idx,
                    chunk_label=curr_chunk.label,
                    status=ChangeStatus.UNCHANGED,
                ))
            else:
                sim = simhash_similarity(prev_chunk.simhash, curr_chunk.simhash)
                changes.append(ChunkChange(
                    chunk_index=idx,
                    chunk_label=curr_chunk.label,
                    status=ChangeStatus.MODIFIED,
                    similarity=sim,
                ))

    return changes
