"""Conditional-GET validator state — store, guard, and decide (#269, parts 2-4).

Watcher does not fetch (Phase 4, #241), so a conditional request is a pair of
headers on the command it issues: ``If-None-Match`` from a stored ``etag``,
``If-Modified-Since`` from a stored ``last_modified``, both replayed **verbatim
and unparsed** — ``W/`` prefix, quotes, and the origin's own date spelling
included. Replicator refuses rather than repairs anything unsendable, so
anything this module lets through is either sent as written or closes the
command as ``invalid_request_options``.

The validators are **per-occasion values on a fingerprint-keyed fact**, so they
live on the *item*, written from the fact that closed its latest command — never
against a fingerprint (issuer contract MUST-5: pinning them to the first fact for
a given fingerprint replays a stale ``If-None-Match`` for exactly as long as the
content is unchanged, which is the period conditional GET was supposed to help).

**Why a 304 needs an invalidation story at all.** A conditional GET that matches
produces no bytes, so nothing is extracted and no fingerprint is recomputed — the
item's fingerprint is inherited from the last 200. That is the point of the
optimisation, but it means a drift introduced by an *extraction* change would go
unnoticed for as long as the origin keeps answering 304. Five of the six rules
below make that deterministic rather than probabilistic; the sixth is the
residual net for what none of them can see.

The rules ``replayable_validators`` applies (listed by subject, not by the order
they are evaluated in — the predicate short-circuits and the order is an
implementation detail):

1. The gate is off for this item (``WATCHER_CONDITIONAL_GET_ENABLED``).
2. The caller forced a full fetch — the operator's "check now".
3. Nothing stored, or nothing sendable once guarded.
4. ``validator_source_key`` disagrees with the item's current key: the URL
   moved, the ``source_specs`` were re-announced, or the extraction generation
   changed (a co-core upgrade, or a bump of ``LOCAL_EXTRACTION_GENERATION``).
   One key rather than a clear scattered across every writer of those fields —
   a path that forgets to call a clear is the failure mode that ends in a
   silently stale fingerprint.
5. No ``last_full_fetch_at`` — unknown provenance is not replayable.
6. The pair is older than ``WATCHER_VALIDATOR_MAX_AGE_HOURS``.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version

from src.core.logging import get_logger

logger = get_logger(__name__)

CONDITIONAL_GET_ENV = "WATCHER_CONDITIONAL_GET_ENABLED"
VALIDATOR_MAX_AGE_ENV = "WATCHER_VALIDATOR_MAX_AGE_HOURS"

# A week. Four items at ~122 KB make a forced full fetch cost nothing, so the
# ceiling is set for confidence rather than for bandwidth.
DEFAULT_VALIDATOR_MAX_AGE_HOURS = 168.0

# Mirrors Replicator's ``MAX_HEADER_VALUE_LENGTH`` (currently 1024): its read
# half already drops a longer value rather than truncating it, so a stored value
# over the bound came from somewhere else and is not replayable either.
MAX_VALIDATOR_LENGTH = 1024

# The distribution that owns extraction (fetch → extract → fingerprint lives in
# co-core; watcher only calls it).
CO_CORE_DISTRIBUTION = "co-core"

# Bumped by hand when *watcher's* own extraction changes in a way co-core's
# version cannot see — how chunks are joined, which extractor a media type
# dispatches to, the spec fallback order.
LOCAL_EXTRACTION_GENERATION = 1


def extraction_generation() -> str:
    """Identity of the extraction that produced a fingerprint (CR-3).

    Two halves. The co-core version arrives through the wheelhouse with no
    human in the loop, so pinning a hand-bumped integer here reproduced the
    ``WATCHER_USER_AGENT`` hazard one step quieter: an extractor change nobody
    bumped for would leave every 304-ing item inheriting a fingerprint the *old*
    extractor computed, invisible until the origin's bytes happened to change.
    Reading the installed version makes an upgrade invalidate every stored
    validator by itself — one full fetch per item, which at this fleet size is
    free. The local half stays for watcher-side extraction changes, which
    co-core's version cannot see.

    A missing distribution degrades to a sentinel rather than raising: the issue
    path must not fall over for a packaging problem, and an unknown version
    simply forces unconditional fetches, which is the safe direction.
    """
    try:
        co_core = version(CO_CORE_DISTRIBUTION)
    except PackageNotFoundError:
        co_core = "unknown"
    return f"{co_core}+{LOCAL_EXTRACTION_GENERATION}"


EXTRACTION_GENERATION = extraction_generation()

# Printable US-ASCII and SP. Narrower than RFC 9110 permits, matching the
# refusal list Replicator applies to a command's ``headers``: this excludes CR,
# LF, NUL, HTAB and all of obs-text (\x80-\xff), which latin-1 header decoding
# can surface on the read half.
_SENDABLE_CHARS = frozenset(chr(c) for c in range(0x20, 0x7F))

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})


def sendable_validator(value: str | None) -> str | None:
    """The value if Replicator will send it as written, else ``None``.

    Refuse-before-minting, mirroring Replicator's own posture. A command whose
    headers are refused closes as a terminal ``invalid_request_options``, which
    on this side is ERROR health plus one ``WATCH_ERROR`` — paid for a header we
    could have declined to send. Nothing *inside* the value is touched: a
    repaired validator is a fetch the operator cannot account for, and a
    truncated ETag is one that can never match.
    """
    if value is None:
        return None
    if not value.strip():
        return None
    if len(value) > MAX_VALIDATOR_LENGTH:
        return None
    if not set(value) <= _SENDABLE_CHARS:
        return None
    return value


def validator_source_key(
    *,
    effective_url: str,
    source_specs: list | None,
    generation: str = EXTRACTION_GENERATION,
) -> str:
    """Identity of "what these bytes were going to mean" when a pair was stored.

    Order-significant over the spec list (the fallback loop tries specs in order,
    so a reorder can bind a different spec) and order-insensitive within each
    spec's keys (``sort_keys``), because a JSONB round-trip does not preserve
    key order and a spurious mismatch costs a full fetch for nothing.
    """
    payload = json.dumps(
        {"url": effective_url, "specs": source_specs or [], "generation": generation},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def conditional_get_enabled(watched_item_id: str) -> bool:
    """Whether this item may send validators.

    Three settings, so the rollout has a canary step: unset or falsey is off for
    everything, a truthy value is on for the fleet, and anything else is read as
    a comma-separated list of WatchedItem ids — the only ones allowed to send.
    """
    raw = os.environ.get(CONDITIONAL_GET_ENV, "").strip()
    lowered = raw.lower()
    if lowered in _FALSEY:
        return False
    if lowered in _TRUTHY:
        return True
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return str(watched_item_id).lower() in allowed


def validator_max_age() -> timedelta:
    """How long a stored pair may be replayed before one full fetch is forced.

    An unparseable value falls back to the default rather than raising: the knob
    must not be able to wedge the issue path, and the default already errs
    toward re-fetching.
    """
    raw = os.environ.get(VALIDATOR_MAX_AGE_ENV)
    if raw is None:
        return timedelta(hours=DEFAULT_VALIDATOR_MAX_AGE_HOURS)
    try:
        hours = float(raw)
    except ValueError:
        logger.warning(
            "unparseable %s — using the default", VALIDATOR_MAX_AGE_ENV, extra={"value": raw}
        )
        return timedelta(hours=DEFAULT_VALIDATOR_MAX_AGE_HOURS)
    if hours <= 0:
        # Safe direction — every command fetches in full — but silent, and a
        # typo'd sign is indistinguishable from a deliberate kill switch unless
        # the effect is said out loud (CR-6).
        logger.info(
            "%s is not positive — conditional GET is effectively disabled",
            VALIDATOR_MAX_AGE_ENV,
            extra={"value": raw},
        )
    return timedelta(hours=hours)


def replayable_validators(
    watched_item,
    *,
    now: datetime,
    force_full_fetch: bool = False,
) -> tuple[str | None, str | None]:
    """The ``(etag, last_modified)`` this occasion may replay — often neither.

    Pure over the item's columns plus two env knobs, so every rule in the module
    docstring is unit-testable without a bus or a database. The caller snapshots
    the result onto the ``FetchCommand`` row rather than re-deriving it at
    publish time: the pending-publish sweep holds only that row.
    """
    if force_full_fetch:
        return (None, None)
    if not conditional_get_enabled(watched_item.id):
        return (None, None)

    etag = sendable_validator(watched_item.etag)
    last_modified = sendable_validator(watched_item.last_modified)
    if etag is None and last_modified is None:
        return (None, None)

    current_key = validator_source_key(
        effective_url=watched_item.effective_url,
        source_specs=watched_item.source_specs,
    )
    if watched_item.validator_source_key != current_key:
        return (None, None)

    last_full_fetch_at = watched_item.last_full_fetch_at
    if last_full_fetch_at is None:
        return (None, None)
    if now - last_full_fetch_at > validator_max_age():
        return (None, None)

    return (etag, last_modified)


def record_validators(
    watched_item,
    *,
    etag: str | None,
    last_modified: str | None,
    now: datetime,
) -> None:
    """Store the pair from the fact that closed the item's latest command.

    Called only from the blob apply path, and only after its ordering guard, so
    a late older fact can never overwrite a newer pair (MUST-5). The fetch stamp
    is ``stamp_full_fetch``'s, not this function's. **Always an overwrite,
    ``None`` included**: the pair must describe the latest 200, and an
    origin that stopped offering a validator must not leave the old one
    replayable against bytes it no longer names.
    """
    watched_item.etag = etag
    watched_item.last_modified = last_modified
    watched_item.validator_source_key = validator_source_key(
        effective_url=watched_item.effective_url,
        source_specs=watched_item.source_specs,
    )


def stamp_full_fetch(watched_item, *, now: datetime) -> None:
    """Record that bytes arrived (CR-2).

    Deliberately separate from ``record_validators``: "we got bytes" is a fetch
    fact, true even when extraction then failed and no pair was stored. Folding
    it into the validator write left the column — rendered as *Last Full Fetch*
    — claiming no bytes had arrived on exactly the cycle where they had.
    """
    watched_item.last_full_fetch_at = now


def clear_validators(watched_item) -> None:
    """Forget the pair, so the next command is unconditional.

    ``last_full_fetch_at`` survives: it records when bytes last arrived, which
    stays true whatever happens to the validators (``stamp_full_fetch``).
    """
    watched_item.etag = None
    watched_item.last_modified = None
    watched_item.validator_source_key = None
