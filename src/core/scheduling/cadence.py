"""Cadence engine — interval parsing and next-check computation.

Temporal-profile evaluation (``resolve_effective_interval``, the per-type
resolvers, ``evaluate_post_actions``) and ``format_interval``, which rendered a
profile cadence, were removed in F2 (#375): Archiver evaluates profiles and
announces the resulting interval (CannObserv/archiver#364, #365).
``validate_optional_schedule_config``, the API's write-boundary check, went
with ``/api/v1`` (#378); the reconcile validates what Archiver announces.
"""

import re
from datetime import UTC, datetime, timedelta

DEFAULT_INTERVAL = timedelta(days=1)
INTERVAL_PATTERN = re.compile(r"^(\d+)([smhd])$")
INTERVAL_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_interval(value: str | None) -> timedelta:
    """Parse a human-readable interval string to timedelta.

    Supported: '30s', '15m', '6h', '1d'. Returns daily default for None.
    """
    if value is None:
        return DEFAULT_INTERVAL
    match = INTERVAL_PATTERN.match(value)
    if not match:
        raise ValueError(f"Invalid interval: {value!r}. Use format like '30s', '15m', '6h', '1d'.")
    amount = int(match.group(1))
    unit = INTERVAL_UNITS[match.group(2)]
    return timedelta(**{unit: amount})


def compute_next_check(
    schedule_config: dict,
    last_checked_at: datetime | None,
    now: datetime | None = None,
) -> datetime:
    """Compute when a watch should next be checked: ``last_checked_at`` plus the
    config's interval, clamped to ``now`` when overdue or never checked."""
    now = now or datetime.now(UTC)
    interval = parse_interval(schedule_config.get("interval"))
    if last_checked_at is None:
        return now
    next_due = last_checked_at + interval
    if next_due <= now:
        return now
    return next_due
