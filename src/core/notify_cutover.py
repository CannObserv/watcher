"""The notification cutover instant T (#376 part 1, F3; epics #368, #394).

F3 hands notifications from Watcher to Archiver at one instant. Watcher
dispatches the events whose ``occurred_at`` is before T; Archiver dispatches
those at or after it (``ARCHIVER_NOTIFY_CUTOVER_AT``, archiver#369 D3). Both
compare the event's own instant — the ``now`` that also stages its
``info.watch-events`` fact — so clock skew between hosts doesn't matter. Only
*both* services holding the same T, restarted before it arrives, makes the
flip free of gaps and double sends.

**Unset is today.** No T, no gate: every event dispatches as before.

**Archiver's grammar, character for character**: ISO 8601 with seconds, an
optional 1–6 digit fraction, and ``Z`` or ``+00:00``. A date alone, a naive
time or any other offset refuses startup — a value only one service accepts,
or that the two read differently, is a gap or a double send nobody sees.

Validated at startup by ``assert_notify_cutover_at``, then read per call like
``src.core.public_base_url``: the process environment cannot change without a
restart, so this is the same T every time, and a test sees its own.

Temporary: part 2 deletes this module with Watcher's dispatch path.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime

from src.core.logging import get_logger

logger = get_logger(__name__)

NOTIFY_CUTOVER_AT_ENV = "WATCHER_NOTIFY_CUTOVER_AT"

#: Archiver's ``_UTC_INSTANT`` (``src/core/notifications/cutover.py``), copied
#: rather than shared: Watcher reads no Archiver code (#311). Changing it on
#: one side alone breaks "same string, same instant".
_UTC_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|\+00:00)")


class NotifyCutoverInvalid(ValueError):
    """``WATCHER_NOTIFY_CUTOVER_AT`` is set, but not to an unambiguous UTC instant."""


def notify_cutover_at(environ: Mapping[str, str]) -> datetime | None:
    """Return T as an aware UTC datetime, or ``None`` when unset or blank.

    Raises ``NotifyCutoverInvalid`` for anything outside the grammar above, and
    for a value that matches its shape but names no instant (month 13, hour 25).
    """
    raw = (environ.get(NOTIFY_CUTOVER_AT_ENV) or "").strip()
    if not raw:
        return None
    if not _UTC_INSTANT.fullmatch(raw):
        raise NotifyCutoverInvalid(
            f"{NOTIFY_CUTOVER_AT_ENV}={raw!r} is not a UTC instant: write "
            "YYYY-MM-DDTHH:MM:SS[.ffffff] followed by Z or +00:00, the same string "
            "as archiver.service's ARCHIVER_NOTIFY_CUTOVER_AT"
        )
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise NotifyCutoverInvalid(
            f"{NOTIFY_CUTOVER_AT_ENV}={raw!r} does not name an instant ({exc})"
        ) from None


def format_instant(cutover: datetime) -> str:
    """An aware instant as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` in UTC, as Archiver logs T."""
    return cutover.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def assert_notify_cutover_at(environ: Mapping[str, str]) -> None:
    """Startup check: refuse a malformed T, and say which T this process holds.

    The INFO line is the read-back for the flip: it is compared with Archiver's
    ``notify cutover:`` line before T arrives.
    """
    cutover = notify_cutover_at(environ)
    if cutover is None:
        logger.info("notify cutover: unset (Watcher dispatches every event)")
    else:
        logger.info(
            "notify cutover: %s (events at or after it are Archiver's)", format_instant(cutover)
        )
