"""Base types for the notification subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ChangeEvent:
    """Immutable value object describing a detected change."""

    watch_id: str
    watch_name: str
    watch_url: str
    change_id: str
    detected_at: datetime
    change_metadata: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """Human-readable one-line summary of the change."""
        meta = self.change_metadata
        parts: list[str] = []
        for label in ("added", "modified", "removed"):
            items = meta.get(label, [])
            if items:
                parts.append(f"{len(items)} {label}")
        detail = ", ".join(parts) if parts else "details pending"
        return f"Change detected: {self.watch_name} — {detail}"


@runtime_checkable
class NotificationChannel(Protocol):
    """Protocol every notification channel must satisfy."""

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """Deliver *event* using channel-specific *config*. Return True on success."""
        ...
