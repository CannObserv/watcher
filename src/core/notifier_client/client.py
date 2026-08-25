"""Env-configured NotifierClient factory and watcher-specific helpers.

A URL is *configuration*, not *permission* (#277) — the rule ``src.core.bus``
states for the broker address (#262) and ``src.core.db_safety`` for the
production database (#233), applied here to the last outbound production
credential that lacked it.

``/etc/watcher/.env`` carries ``WATCHER_NOTIFIER_BASE_URL`` and ``WATCHER_NOTIFIER_API_KEY``,
and AGENTS.md tells every agent to ``source scripts/load-env.sh`` before
pytest. Before the gate below, that was enough to dispatch for real: the
factory read both variables straight from ``os.environ`` and refused only when
they were *unset*, so a suite, a hand-run dev server, a one-off script or a
REPL in a prod-sourced shell notified the production tenant — and *succeeded*,
which is why it left no error to notice. The blast radius is not a stray row in
a database: a notification is delivered to real subscribers on real channels,
and cannot be recalled.

``WATCHER_NOTIFIER_ENABLED=1`` is therefore additionally required to build a client at
all. Only ``deploy/watcher.service`` and ``scripts/dev_server.sh``'s
scratch-notifier branch set it — never an env file, for the same reason as
``WATCHER_ALLOW_PRODUCTION_DB`` and ``WATCHER_BUS_ENABLED``: an env file is
precisely what the unsanctioned launch paths source.
"""

import os
from collections.abc import Mapping

from notifier_client import NotifierClient

from src.core.notifications.events import WatchEvent

WATCHER_NOTIFIER_BASE_URL_ENV = "WATCHER_NOTIFIER_BASE_URL"
WATCHER_NOTIFIER_API_KEY_ENV = "WATCHER_NOTIFIER_API_KEY"

#: Unit-only opt-in gating every notifier client this process can build (#277).
WATCHER_NOTIFIER_ENABLED_ENV = "WATCHER_NOTIFIER_ENABLED"


class NotifierNotEnabled(RuntimeError):
    """Raised when a process holds a notifier URL it was never authorised to use."""


def notifier_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when the caller explicitly opted this process into the notifier.

    Only the exact string ``"1"`` opts in. A fuzzy truthiness check would let a
    stray value quietly re-open the hole the gate closes — the same rule
    ``src.core.bus`` and ``src.core.db_safety`` apply to their flags.
    """
    if environ is None:
        environ = os.environ
    return environ.get(WATCHER_NOTIFIER_ENABLED_ENV) == "1"


def assert_environment_notifier_allowed(environ: Mapping[str, str]) -> None:
    """Refuse a notifier URL that was never opted into (#277, the loud half).

    :func:`get_notifier_client` already fails *closed* on this combination, but
    closed-and-silent trades one production hazard for another: drop
    ``Environment=WATCHER_NOTIFIER_ENABLED=1`` from the unit and Watcher stops notifying
    with nothing but a per-dispatch error to say so — and unlike a missed
    publish, a missed notification is the whole point of the service. A URL
    present without the opt-in is always a mistake in either direction — a
    service that lost its flag, or a process that should never have had the URL
    — so the entry point refuses to start.

    Not raised when the URL is absent: that names no notifier, so nothing can be
    dispatched by accident, and making it fatal would stop every dev server and
    script that never wanted one.

    Takes ``environ`` explicitly, like its two siblings: the caller decides what
    is being gated, and this stays callable from a test even though
    ``tests/conftest.py`` clears the real variables at import.
    """
    if not environ.get(WATCHER_NOTIFIER_BASE_URL_ENV):
        return
    if notifier_enabled(environ):
        return
    raise NotifierNotEnabled(
        f"refusing to start: {WATCHER_NOTIFIER_BASE_URL_ENV} is set but "
        f"{WATCHER_NOTIFIER_ENABLED_ENV} is not 1, so this process holds a "
        "notifier address it is not authorised to use.\n"
        f"  Only deploy/watcher.service may notify the production tenant; it sets "
        f"{WATCHER_NOTIFIER_ENABLED_ENV}=1. If this IS the service, that line is missing from "
        "the installed unit.\n"
        "  For a dev server use: bash scripts/dev_server.sh (set "
        "WATCHER_DEV_NOTIFIER_BASE_URL for a scratch notifier).\n"
        f"  For anything else, unset {WATCHER_NOTIFIER_BASE_URL_ENV}. (See #277.)"
    )


def get_notifier_client() -> NotifierClient:
    """Return a NotifierClient configured from env vars.

    Raises RuntimeError if WATCHER_NOTIFIER_BASE_URL or WATCHER_NOTIFIER_API_KEY are unset, and
    :class:`NotifierNotEnabled` if they are set without ``WATCHER_NOTIFIER_ENABLED=1``.
    Each call creates a new client — callers that fan out to multiple channels
    should create one client and reuse it for the lifetime of the operation.

    The two "unset" checks come before the gate so the operator reads the
    variable that is actually missing rather than the one the check happened to
    test first — the ordering rule ``bus_disabled_reason`` follows.
    """
    base_url = os.environ.get(WATCHER_NOTIFIER_BASE_URL_ENV)
    if not base_url:
        raise RuntimeError(f"{WATCHER_NOTIFIER_BASE_URL_ENV} environment variable is required")
    api_key = os.environ.get(WATCHER_NOTIFIER_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{WATCHER_NOTIFIER_API_KEY_ENV} environment variable is required")
    assert_environment_notifier_allowed(os.environ)
    return NotifierClient(base_url=base_url, api_key=api_key)


def build_idempotency_key(event: WatchEvent, source_id: str) -> str:
    """Build a stable, tenant-scoped idempotency key for a notification dispatch.

    For change_detected: keyed by (event_type, source_id, change_revision_id) —
    stable across retries of the same change, unique per source.
    For all other events: keyed by (event_type, source_id, watched_item_id, occurred_at_ms)
    — stable within a millisecond window, unique per source.
    """
    change_revision_id = event.metadata.get("change_revision_id")
    if change_revision_id:
        return f"watcher:{event.event_type.value}:{source_id}:{change_revision_id}"
    occurred_ms = int(event.occurred_at.timestamp() * 1000)
    return f"watcher:{event.event_type.value}:{source_id}:{event.watched_item_id}:{occurred_ms}"
