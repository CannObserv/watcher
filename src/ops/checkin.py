"""Report a backup run to notifier's dead-man monitor (#296 D9).

A backup that fails loudly still says nothing when it stops running: a disabled
timer, a dead VM and a wedged interpreter all produce zero failures and zero
traffic. So the job checks in on **every** run, success or not, and notifier
alarms when a check-in fails to arrive — broker#3's design, the one whose alarm
was proven to fire rather than assumed to (notifier#56).

``POST /api/v1/monitors/{id}/checkin`` with ``{"status": "ok"|"alert",
"variables": {...}}``. An ``ok`` dispatches nothing and resets the timer; an
``alert`` renders the monitor's template against ``variables``. Retry-safe by
contract: a replay overwrites the previous check-in.

**The host is configuration**, and here watcher departs from broker#3, which
made it a constant so a one-character port typo could not point a dead-man's
switch at notifier_dev (``:9001``, whose ``/health`` is byte-identical). This
repo's rule wins in this repo: a notifier URL in ``src/`` is a real connection
target, so a literal is the defect whatever host it names
(``tests/test_notifier_isolation.py``, #280). And notifier#56 found the typo is
not silent anyway: keys live per database, so a production-marked key sent to
``:9001`` is a **401** — no check-in lands, and the monitor (anchored on its own
creation until the first check-in) alarms. Inverting the alarm takes the wrong
port *and* a development-marked key: a two-fault path, not a slip.

**Failure never propagates.** A check-in never raises and never changes the
job's exit status: a monitoring path that fails the thing it monitors trains an
operator to ignore both.

All three values live in ``/etc/watcher/backup-notifier.env`` (0400 root:root),
loaded by the backup unit alone. ``/etc/watcher/notifier.env`` stays
``watcher.service``'s and nothing else's (#278).
"""

import re
from collections.abc import Callable, Mapping
from typing import Literal
from urllib.parse import urlsplit

import httpx

from src.core.logging import get_logger

logger = get_logger(__name__)

BASE_URL_ENV = "WATCHER_BACKUP_NOTIFIER_BASE_URL"
MONITOR_ID_ENV = "WATCHER_BACKUP_MONITOR_ID"
API_KEY_ENV = "WATCHER_BACKUP_NOTIFIER_API_KEY"
_ALL_ENV = (BASE_URL_ENV, MONITOR_ID_ENV, API_KEY_ENV)
TIMEOUT_SECONDS = 10.0

#: One immediate retry on a transport error or a 5xx. A dropped check-in reads as
#: a dead job, and the replay is harmless (notifier#56).
_ATTEMPTS = 2
#: Monitor ids are ULIDs; the value becomes a URL path segment.
_MONITOR_ID_RE = re.compile(r"^[0-9A-Za-z]+$")

Post = Callable[[str, dict, dict, float], int]


def _http_post(url: str, payload: dict, headers: dict, timeout: float) -> int:
    """One POST, returning the status code. The seam the tests replace."""
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, json=payload, headers=headers).status_code


def _is_http_base(base: str) -> bool:
    """An http(s) URL with a host and, if any, a numeric port. ``urlsplit``
    raises on a bad bracket and ``.port`` on a bad port; both are a typo."""
    try:
        parts = urlsplit(base)
        _ = parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def post_checkin(
    status: Literal["ok", "alert"],
    variables: dict,
    *,
    environ: Mapping[str, str],
    post: Post = _http_post,
) -> bool:
    """Report this run; return whether the check-in landed. Never raises.

    The guarantee is this wrapper's, not the callee's care: whatever escapes the
    configuration checks — a non-ASCII key failing header encoding, a URL httpx
    parses differently from ``urlsplit`` — is logged by type (never by message,
    which may quote the value) and reported as a check-in that did not land.
    """
    try:
        return _post_checkin(status, variables, environ=environ, post=post)
    except Exception as e:
        logger.error("backup check-in failed: %s — not checking in", type(e).__name__)
        return False


def _post_checkin(
    status: Literal["ok", "alert"],
    variables: dict,
    *,
    environ: Mapping[str, str],
    post: Post,
) -> bool:
    values = {name: environ.get(name, "").strip() for name in _ALL_ENV}
    if not any(values.values()):
        logger.warning(
            "backup check-in not configured (%s unset) — a stopped backup will not be noticed",
            ", ".join(_ALL_ENV),
        )
        return False
    if not all(values.values()):
        logger.error(
            "backup check-in half-configured — %s must all be set; not checking in",
            ", ".join(_ALL_ENV),
            extra={f"has_{name.lower()}": bool(value) for name, value in values.items()},
        )
        return False
    base, monitor_id, api_key = (values[name] for name in _ALL_ENV)
    if not _is_http_base(base):
        logger.error("%s is not an http(s) URL with a host; not checking in", BASE_URL_ENV)
        return False
    if not _MONITOR_ID_RE.match(monitor_id):
        logger.error("%s is not a bare monitor id; not checking in", MONITOR_ID_ENV)
        return False

    url = f"{base.rstrip('/')}/api/v1/monitors/{monitor_id}/checkin"
    payload = {"status": status, "variables": variables}
    headers = {"X-API-Key": api_key}
    failure = ""
    for _ in range(_ATTEMPTS):
        try:
            code = post(url, payload, headers, TIMEOUT_SECONDS)
        except httpx.TransportError as e:
            failure = f"{type(e).__name__}: {e}"
            continue
        if 200 <= code < 300:
            return True
        if code < 500:
            logger.warning(
                "backup check-in rejected with HTTP %s — check the monitor id and key", code
            )
            return False
        failure = f"HTTP {code}"
    logger.warning("backup check-in failed: %s", failure)
    return False
