"""Scheduler logic — compute when watches are due for checking."""

import re
from datetime import UTC, date, datetime, timedelta

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


def _to_date(value: str | date) -> date:
    """Coerce string or date to date object."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _resolve_date_profile(profile: dict, today: date) -> timedelta | None:
    """Resolve event/deadline profile to interval. None if past or no rule matches."""
    ref = _to_date(profile["reference_date"])
    if today > ref:
        return None
    days_until = (ref - today).days
    # Find tightest matching rule (smallest days_before that still covers days_until)
    best_rule = None
    for rule in profile["rules"]:
        if days_until <= rule["days_before"]:
            if best_rule is None or rule["days_before"] < best_rule["days_before"]:
                best_rule = rule
    if best_rule is None:
        return None
    return parse_interval(best_rule["interval"])


def _resolve_seasonal_profile(profile: dict, today: date) -> timedelta | None:
    """Resolve seasonal profile to interval. None if outside date range."""
    start = _to_date(profile["date_range_start"])
    end = _to_date(profile["date_range_end"])
    if start <= today <= end:
        return parse_interval(profile["rules"][0]["interval"])
    return None


def _resolve_single_profile(profile: dict, today: date) -> timedelta | None:
    """Dispatch to type-specific resolver."""
    ptype = profile.get("type")
    if ptype in ("event", "deadline"):
        return _resolve_date_profile(profile, today)
    if ptype == "seasonal":
        return _resolve_seasonal_profile(profile, today)
    return None


def resolve_effective_interval(
    profiles: list[dict],
    today: date | None = None,
) -> timedelta | None:
    """Evaluate temporal profiles, return shortest applicable interval or None."""
    today = today or date.today()
    shortest = None
    for profile in profiles:
        if not profile.get("is_active", True):
            continue
        interval = _resolve_single_profile(profile, today)
        if interval is not None:
            if shortest is None or interval < shortest:
                shortest = interval
    return shortest


def compute_next_check(
    schedule_config: dict,
    last_checked_at: datetime | None,
    now: datetime | None = None,
    profiles: list[dict] | None = None,
) -> datetime:
    """Compute when a watch should next be checked."""
    now = now or datetime.now(UTC)
    profile_interval = None
    if profiles:
        profile_interval = resolve_effective_interval(profiles, today=now.date())
    interval = profile_interval or parse_interval(schedule_config.get("interval"))
    if last_checked_at is None:
        return now
    next_due = last_checked_at + interval
    if next_due <= now:
        return now
    return next_due


def evaluate_post_actions(
    profiles: list[dict],
    today: date | None = None,
) -> list[dict]:
    """Return post-event actions for profiles whose date has passed."""
    raise NotImplementedError
