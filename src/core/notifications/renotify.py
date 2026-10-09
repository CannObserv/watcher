"""Re-notification of a persistent ``WATCH_ERROR`` (#71).

``WATCH_ERROR`` fires on the OK→ERROR transition; without this, a permanently
dead item is reported exactly once, ever. A failed check on an item already in
ERROR re-sends it once the item's ``last_error_notified_at`` is at least
``WATCHER_ERROR_RENOTIFY_INTERVAL`` old.

**A service-wide knob, not ``schedule_config``.** Resolution returns the winning
tier's dict whole, and announced configs are registry-owned (#254): an interval
set there would vanish the moment the registry announced a cadence. The
per-item state is ``last_error_notified_at`` — Watcher-owned, so it survives
reconciliation.
"""

import os
from datetime import datetime, timedelta

from src.core.logging import get_logger
from src.core.scheduling.cadence import parse_interval
from src.core.utils import format_utc_iso

logger = get_logger(__name__)

ERROR_RENOTIFY_INTERVAL_ENV = "WATCHER_ERROR_RENOTIFY_INTERVAL"
DEFAULT_ERROR_RENOTIFY_INTERVAL = "24h"


def error_renotify_interval() -> timedelta:
    """How long an item stays in ERROR between one ``WATCH_ERROR`` and the next.

    Same '30s'/'15m'/'6h'/'1d' vocabulary as a cadence. An unparseable value
    falls back to the default rather than raising, as ``validator_max_age``
    does: it is read on the failure path, and a ``ValueError`` there would
    leave the check unrecorded on every pass.
    """
    raw = os.environ.get(ERROR_RENOTIFY_INTERVAL_ENV)
    if raw is None:
        return parse_interval(DEFAULT_ERROR_RENOTIFY_INTERVAL)
    try:
        interval = parse_interval(raw.strip())
    except ValueError:
        logger.warning(
            "unparseable %s — using the default",
            ERROR_RENOTIFY_INTERVAL_ENV,
            extra={"value": raw},
        )
        return parse_interval(DEFAULT_ERROR_RENOTIFY_INTERVAL)
    if not interval:
        # CR-6's rule: a typo is indistinguishable from intent unless the
        # effect is said out loud — and this one is loud for recipients.
        logger.info(
            "%s is zero — every failed check of an ERROR item re-notifies",
            ERROR_RENOTIFY_INTERVAL_ENV,
            extra={"value": raw},
        )
    return interval


def error_renotify_due(
    last_error_notified_at: datetime | None, *, now: datetime, interval: timedelta
) -> bool:
    """Whether an item already in ERROR is owed another ``WATCH_ERROR``.

    ``None`` is due: an ERROR item with no record of anyone being told must not
    stay silent for ever. The migration stamps the rows that were already in
    ERROR, so this is not a deploy-time burst.
    """
    return last_error_notified_at is None or now - last_error_notified_at >= interval


def error_renotify_metadata(*, repeat: bool, previously_notified_at: datetime | None) -> dict:
    """The keys a ``WATCH_ERROR`` carries to say whether it is a repeat.

    ``renotify`` is on every one, so a template can branch on it under the
    preview's strict rendering. ``previously_notified_at`` is the last time
    anyone was told — on the first repeat, roughly when the failure began. It is
    omitted rather than invented when there is no record.
    """
    meta: dict = {"renotify": repeat}
    if repeat and previously_notified_at is not None:
        meta["previously_notified_at"] = format_utc_iso(previously_notified_at)
    return meta
