"""Storage backend protocol and local filesystem implementation."""

from pathlib import Path
from typing import Protocol


class StorageBackend(Protocol):
    """Protocol for content storage backends."""

    def save(self, path: str, content: bytes) -> None: ...
    def load(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def snapshot_path(self, watch_id: str, snapshot_id: str, extension: str) -> str: ...


class LocalStorage:
    """Store content on the local filesystem."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def save(self, path: str, content: bytes) -> None:
        """Save content to a relative path under base_dir."""
        full_path = self.base_dir / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)

    def load(self, path: str) -> bytes:
        """Load content from a relative path. Raises FileNotFoundError if missing."""
        full_path = self.base_dir / path
        return full_path.read_bytes()

    def exists(self, path: str) -> bool:
        """Check if a path exists."""
        return (self.base_dir / path).is_file()

    def snapshot_path(self, watch_id: str, snapshot_id: str, extension: str) -> str:
        """Build the conventional storage path for a snapshot."""
        return f"snapshots/{watch_id}/{snapshot_id}.{extension}"
