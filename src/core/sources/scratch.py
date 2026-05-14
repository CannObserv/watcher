"""Scratch-file management for SourceRevision content."""

import os
from pathlib import Path

from ulid import ULID

DEFAULT_CACHE_DIR = "/var/cache/watcher/scratch"


def _cache_dir() -> Path:
    d = Path(os.environ.get("WATCHER_CACHE_DIR", DEFAULT_CACHE_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d


def allocate_revision_id() -> str:
    """Return a fresh ULID string suitable for `source_revision_id`."""
    return str(ULID())


def scratch_path_for(revision_id: str) -> Path:
    return _cache_dir() / f"{revision_id}.bin"


def write_scratch_bytes(revision_id: str, content: bytes) -> Path:
    """Write content to <cache_dir>/<revision_id>.bin atomically."""
    target = scratch_path_for(revision_id)
    tmp = target.with_suffix(".bin.tmp")
    tmp.write_bytes(content)
    tmp.replace(target)
    return target


def rename_scratch_to_canonical(allocated_id: str, canonical_id: str) -> Path:
    """Rename allocated → canonical when server returned a different ULID."""
    if allocated_id == canonical_id:
        return scratch_path_for(canonical_id)
    source = scratch_path_for(allocated_id)
    target = scratch_path_for(canonical_id)
    if target.exists():
        source.unlink(missing_ok=True)
        return target
    source.rename(target)
    return target
