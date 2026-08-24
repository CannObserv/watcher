"""Watcher adapter over notifier-client SDK.

Thin wrapper that provides an env-configured NotifierClient and watcher-specific
helpers (idempotency key construction). All network and retry logic lives in the
SDK; this module handles watcher-domain concerns only.

Also owns the ``NOTIFIER_ENABLED=1`` gate (#277) — a notifier URL inherited from
an env file is configuration, not permission. See ``client.py``.
"""

from src.core.notifier_client.client import (
    NOTIFIER_API_KEY_ENV,
    NOTIFIER_BASE_URL_ENV,
    NOTIFIER_ENABLED_ENV,
    NotifierNotEnabled,
    assert_environment_notifier_allowed,
    build_idempotency_key,
    get_notifier_client,
    notifier_enabled,
)

__all__ = [
    "NOTIFIER_API_KEY_ENV",
    "NOTIFIER_BASE_URL_ENV",
    "NOTIFIER_ENABLED_ENV",
    "NotifierNotEnabled",
    "assert_environment_notifier_allowed",
    "build_idempotency_key",
    "get_notifier_client",
    "notifier_enabled",
]
