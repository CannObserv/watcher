"""Watcher adapter over notifier-client SDK.

Thin wrapper that provides an env-configured NotifierClient and watcher-specific
helpers (idempotency key construction). All network and retry logic lives in the
SDK; this module handles watcher-domain concerns only.

Also owns the ``WATCHER_NOTIFIER_ENABLED=1`` gate (#277) — a notifier URL inherited from
an env file is configuration, not permission. See ``client.py``.
"""

from src.core.notifier_client.client import (
    WATCHER_NOTIFIER_API_KEY_ENV,
    WATCHER_NOTIFIER_BASE_URL_ENV,
    WATCHER_NOTIFIER_ENABLED_ENV,
    NotifierCredentialMissing,
    NotifierNotEnabled,
    assert_environment_notifier_allowed,
    build_idempotency_key,
    get_notifier_client,
    notifier_enabled,
)

__all__ = [
    "WATCHER_NOTIFIER_API_KEY_ENV",
    "WATCHER_NOTIFIER_BASE_URL_ENV",
    "WATCHER_NOTIFIER_ENABLED_ENV",
    "NotifierCredentialMissing",
    "NotifierNotEnabled",
    "assert_environment_notifier_allowed",
    "build_idempotency_key",
    "get_notifier_client",
    "notifier_enabled",
]
