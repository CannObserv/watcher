"""Cadence engine — interval parsing, validation, next-check computation.

Temporal-profile evaluation (``resolve_effective_interval``, the per-type
resolvers, ``evaluate_post_actions``) and ``format_interval``, which rendered a
profile cadence, were removed in F2 (#375): Archiver evaluates profiles and
announces the resulting interval (CannObserv/archiver#364, #365).
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


def validate_optional_schedule_config(config: dict | None) -> dict | None:
    """Validate an optional cadence schedule_config at a write boundary (#205).

    ``None`` means "inherit / clear" and passes through. A non-``None`` config
    must carry a parseable ``interval`` — an empty dict (or a missing/blank
    interval) is rejected so it can't create a meaningless intervalless tier,
    and a malformed interval string is caught here instead of crashing the
    scheduler. Returns the config unchanged on success; raises ``ValueError``
    otherwise.
    """
    if config is None:
        return None
    interval = config.get("interval")
    if not interval:
        raise ValueError("schedule config must include a non-empty 'interval'")
    if not isinstance(interval, str):
        # Guard before parse_interval — a non-str (e.g. a JSON number) would raise
        # TypeError there, which Pydantic does not convert to a 422 (it escapes as
        # a 500). Raising ValueError keeps the write boundary returning 422/400.
        raise ValueError("schedule config 'interval' must be a string (e.g. '6h', '1d')")
    parse_interval(interval)  # raises ValueError on a malformed interval string
    return config


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
