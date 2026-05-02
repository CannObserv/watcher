"""Env-configured NotifierClient factory and watcher-specific helpers."""

import os

from notifier_client import NotifierClient

from src.core.notifications.events import WatchEvent


def get_notifier_client() -> NotifierClient:
    """Return a NotifierClient configured from env vars.

    Raises RuntimeError if NOTIFIER_BASE_URL or NOTIFIER_API_KEY are unset.
    Each call creates a new client — callers that fan out to multiple channels
    should create one client and reuse it for the lifetime of the operation.
    """
    base_url = os.environ.get("NOTIFIER_BASE_URL")
    if not base_url:
        raise RuntimeError("NOTIFIER_BASE_URL environment variable is required")
    api_key = os.environ.get("NOTIFIER_API_KEY")
    if not api_key:
        raise RuntimeError("NOTIFIER_API_KEY environment variable is required")
    return NotifierClient(base_url=base_url, api_key=api_key)


def build_idempotency_key(event: WatchEvent, source_id: str) -> str:
    """Build a stable, tenant-scoped idempotency key for a notification dispatch.

    For change_detected: keyed by (event_type, source_id, change_id) — stable
    across retries of the same change, unique per source.
    For all other events: keyed by (event_type, source_id, watch_id, occurred_at_ms)
    — stable within a millisecond window, unique per source.
    """
    change_id = event.metadata.get("change_id")
    if change_id:
        return f"watcher:{event.event_type.value}:{source_id}:{change_id}"
    occurred_ms = int(event.occurred_at.timestamp() * 1000)
    return f"watcher:{event.event_type.value}:{source_id}:{event.watch_id}:{occurred_ms}"
