"""Watcher adapter over notifier-client SDK.

Thin wrapper that provides an env-configured NotifierClient and watcher-specific
helpers (idempotency key construction). All network and retry logic lives in the
SDK; this module handles watcher-domain concerns only.
"""

from src.core.notifier_client.client import build_idempotency_key, get_notifier_client

__all__ = ["build_idempotency_key", "get_notifier_client"]
