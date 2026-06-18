"""Scratch-file management for SourceRevision content."""

import os
from pathlib import Path

DEFAULT_CACHE_DIR = "/var/cache/watcher/scratch"


def _cache_dir() -> Path:
    d = Path(os.environ.get("WATCHER_CACHE_DIR", DEFAULT_CACHE_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d


def scratch_path_for(revision_id: str) -> Path:
    return _cache_dir() / f"{revision_id}.bin"


def write_scratch_bytes(revision_id: str, content: bytes) -> Path:
    """Write content to <cache_dir>/<revision_id>.bin atomically."""
    target = scratch_path_for(revision_id)
    tmp = target.with_suffix(".bin.tmp")
    tmp.write_bytes(content)
    tmp.replace(target)
    return target
