"""The content.fetch issue path — Watcher as Replicator's command issuer (#241).

Since the Phase-4 cutover this is the only path by which Watcher obtains
content: it makes no origin request of its own.

The contract this implements is normative in the Replicator repo
(``docs/contracts/content-fetch-issuer-contract.md``); the MUSTs that land in
this module:

* **MUST-1** — ``mint_id`` per fetch *occasion*: a fresh ULID per call, never
  derived from the item or URL (a resource-stable id makes every re-fetch
  inside Replicator's 24 h dedupe TTL a silent no-op).
* **MUST-2** — persist-before-publish: ``create_fetch_command`` writes the
  ``command_id → WatchedItem`` row for the caller to **commit before** any
  XADD. A crash between commit and publish leaves ``pending_publish``, which
  the sweep republishes under the *same* id — Replicator's dedupe makes the
  replay a no-op. Since cannobserv#300 this is bookkeeping rather than
  correctness (the wire carries the domain key) — but the row still holds what
  the wire does not, so nothing about the order changes.
* **replicator#11** — the command pins watcher's User-Agent so the cutover is
  UA-neutral and fingerprints stay byte-continuous.
* **cannobserv#300** — every command names its ``info_source_id``, snapshotted
  onto the row at issue time so the sweep can republish without a WatchedItem.

* **#269** — the command replays the item's stored conditional-GET validators
  (``If-None-Match`` / ``If-Modified-Since``), **snapshotted onto the row at
  issue** rather than read from the item at publish: the sweep holds only the
  row. Which occasions may replay, and which must re-fetch in full, is
  ``src/core/validators.py``; the gate is off until an item is named in
  ``WATCHER_CONDITIONAL_GET_ENABLED``. Safe only because replicator#17 (the
  ``not_modified`` outcome) and #249 part 1 (Watcher's handling of it) are both
  in production — a validator sent to a deployment that still classifies 304 as
  a plain fetch failure reproduces the trap #249 closed.
* **#414** — the command sends the item's ``render`` mode, snapshotted onto the
  row at issue like the validators. Under ``browser`` the command carries
  ``user-agent`` alone: ``replayable_validators`` resolves no pair, because the
  Emit refuses one and would raise on every republish. NULL sends ``None``,
  which Replicator reads as ``http`` — the pre-#414 frame, byte for byte.
* **#457** — the command carries Archiver's fetch-header policy for the source
  (``RegistryAnnouncementState.fetch_headers`` / ``fetch_credential_headers``,
  cannobserv#780), snapshotted onto the row at issue like ``render``. The merge
  is Archiver's D3 (archiver#376), Watcher's half: the pinned UA, then the
  announced plain map (an announced ``user-agent`` in any case **replaces** the
  pin), then the replayed validators; the credential map is copied as
  ``credential_headers``. Names go lower-cased; values verbatim. **A set Watcher
  cannot send blocks the fetch** (``fetch_policy_refusal``): no command, a
  recorded ``fetch_policy_unsendable`` check failure instead. Fetching without
  it could send no credential, or a stale one.
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime

from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import to_wire
from co_core.pure.models.changes import ContentFetchCommandEmit
from co_core_aio.bus import AsyncBusPublisher
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.fetch_command import OPEN_STATUSES, FetchCommand, FetchCommandStatus
from src.core.models.watched_item import WatchedItem
from src.core.validators import replayable_validators

logger = get_logger(__name__)

# Watcher's User-Agent, sent on every command's ``headers`` (replicator#11).
# **Load-bearing for change detection:** fingerprints are UA-sensitive, so this
# value must keep matching what the pre-cutover inline fetcher sent, or every
# watched item reports a spurious change on its next check. Do not "tidy" it.
WATCHER_USER_AGENT = "watcher/0.1.0"

FETCH_COMMAND_TIMEOUT_ENV = "WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS"
DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS = 1800.0
FETCH_MAX_REISSUES_ENV = "WATCHER_FETCH_MAX_REISSUES"
DEFAULT_FETCH_MAX_REISSUES = 3


# The names Watcher replays itself (#269): policy never names one
# (``RegistryAnnouncementEmit`` refuses it), so one in a decoded map is refused
# here rather than sent on every occasion that has no pair to replay.
_VALIDATOR_NAMES = frozenset({"if-none-match", "if-modified-since"})

# What the refusal probe builds the Emit around: only the policy and the mode
# are under test, so the rest is a fixed, valid stand-in.
_PROBE_AT = datetime(2026, 1, 1, tzinfo=UTC)


class UnsendableFetchPolicy(ValueError):
    """The item's fetch-header policy cannot go on a command (#457).

    The message names headers, never a value: it reaches the log, the audit
    row and nothing else.
    """


def command_headers(
    *,
    fetch_headers: dict | None,
    request_etag: str | None,
    request_last_modified: str | None,
) -> dict[str, str]:
    """The command's ``headers``: pinned UA < announced policy < validators (#457 D3).

    Lower-cased names, one line each. Replicator folds case before merging, but
    "issuer wins" is only a rule if exactly one field line goes on the wire —
    which is also why an announced ``User-Agent`` replaces the pin rather than
    riding beside it. Values are sent **verbatim and unparsed**: the validators'
    ``W/`` prefix, quotes and date spelling included (the send-side guard at
    snapshot time already refused anything Replicator would not send), and the
    policy's as Archiver announced them.
    """
    headers = {"user-agent": WATCHER_USER_AGENT}
    for name, value in (fetch_headers or {}).items():
        headers[name.lower()] = value
    if request_etag:
        headers["if-none-match"] = request_etag
    if request_last_modified:
        headers["if-modified-since"] = request_last_modified
    return headers


def command_credential_headers(fetch_credential_headers: dict | None) -> dict[str, str] | None:
    """The command's ``credential_headers``: the announced map, names lower-cased.

    The references are copied byte for byte; Replicator resolves them on its own
    host (cannobserv#577). ``None`` and ``{}`` both send ``None``, no credential.
    """
    if not fetch_credential_headers:
        return None
    return {name.lower(): reference for name, reference in fetch_credential_headers.items()}


def _policy_shape_refusal(label: str, policy: dict) -> str | None:
    """What Watcher refuses before the Emit sees the map (#457 C3).

    A non-string value (the decode is untyped by design), a name twice by case
    (lower-casing would keep one value silently), and a validator.
    """
    not_strings = sorted(name for name, value in policy.items() if not isinstance(value, str))
    if not_strings:
        return f"{label} has a value that is not a string: {', '.join(not_strings)}"
    folded: dict[str, list[str]] = {}
    for name in policy:
        folded.setdefault(name.lower(), []).append(name)
    twice = sorted(spelling for group in folded.values() if len(group) > 1 for spelling in group)
    if twice:
        return f"{label} names one header twice: {', '.join(twice)}"
    validators = sorted(name for name in policy if name.lower() in _VALIDATOR_NAMES)
    if validators:
        return (
            f"{label} names a conditional validator, which Watcher replays itself: "
            f"{', '.join(validators)}"
        )
    return None


def fetch_policy_refusal(
    *,
    fetch_headers: dict | None,
    fetch_credential_headers: dict | None,
    render: str | None,
) -> str | None:
    """Why this policy cannot go on a command under ``render``, or ``None``.

    Watcher's own checks first (``_policy_shape_refusal``), then
    ``ContentFetchCommandEmit`` over the merged set — the same rules the publish
    meets, so a row minted after this passes cannot raise there. The validators
    are left out of the probe: under ``browser`` none is replayed, under ``http``
    the Emit refuses none, and policy may name none (above), so they cannot
    change the verdict.

    Names only, never a value: the Emit is built with ``hide_input_in_errors``
    (cannobserv#577), and its messages name headers and the reference rule.
    """
    plain = fetch_headers or {}
    credentials = fetch_credential_headers or {}
    if not plain and not credentials:
        return None
    for label, policy in (("fetch_headers", plain), ("fetch_credential_headers", credentials)):
        refusal = _policy_shape_refusal(label, policy)
        if refusal is not None:
            return refusal
    try:
        ContentFetchCommandEmit(
            occurred_at=_PROBE_AT,
            command_id="probe",
            url="https://probe.invalid/",
            info_source_id="probe",
            headers=command_headers(
                fetch_headers=plain, request_etag=None, request_last_modified=None
            ),
            credential_headers=credentials,
            render=render,
        )
    except ValidationError as exc:
        return "; ".join(
            error["msg"] for error in exc.errors(include_url=False, include_input=False)
        )
    return None


def _command_headers(row: FetchCommand) -> dict[str, str]:
    """The row's ``headers``: its snapshotted policy and validators, merged."""
    return command_headers(
        fetch_headers=row.fetch_headers,
        request_etag=row.request_etag,
        request_last_modified=row.request_last_modified,
    )


async def create_fetch_command(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    now: datetime,
    intent_id: str | None = None,
    reissue_count: int = 0,
    force_full_fetch: bool = False,
    check_command_id: str | None = None,
) -> FetchCommand:
    """Persist the pending command row (MUST-2: caller commits before publishing).

    ``intent_id`` defaults to a fresh ULID (a new intent); the reaper passes the
    prior command's ``intent_id`` on re-issue so lineage survives the id change.

    The conditional-GET validators are resolved **here**, at the occasion, and
    stored on the row (#269): the pending-publish sweep republishes from the row
    alone, so a value re-read at publish time could differ from what the command
    was minted to ask. ``force_full_fetch`` is the operator's "check now" — an
    unconditional re-read, whatever is stored. ``check_command_id`` names the
    ``watch_check`` command the occasion answers (#371).

    Raises ``UnsendableFetchPolicy`` **before** any row exists when the item's
    fetch-header policy cannot go on a command (#457). A row minted with it
    would raise at every publish and sit ``pending_publish`` for ever, holding
    the open-command gate shut; the caller records a check failure instead.
    """
    refusal = fetch_policy_refusal(
        fetch_headers=watched_item.fetch_headers,
        fetch_credential_headers=watched_item.fetch_credential_headers,
        render=watched_item.render,
    )
    if refusal is not None:
        raise UnsendableFetchPolicy(refusal)
    request_etag, request_last_modified = replayable_validators(
        watched_item, now=now, force_full_fetch=force_full_fetch
    )
    row = FetchCommand(
        command_id=str(ULID()),  # MUST-1: per occasion, never per resource
        intent_id=intent_id if intent_id is not None else str(ULID()),
        watched_item_id=watched_item.id,
        url=watched_item.effective_url,
        # Snapshotted at the occasion so the sweep — which holds no WatchedItem
        # — can publish a valid command (cannobserv#300; NOT NULL since #251).
        info_source_id=watched_item.archiver_info_source_id,
        status=FetchCommandStatus.PENDING_PUBLISH,
        issued_at=now,
        reissue_count=reissue_count,
        request_etag=request_etag,
        request_last_modified=request_last_modified,
        forced_full_fetch=force_full_fetch,
        check_command_id=check_command_id,
        # Snapshotted like the validators (#414): the sweep republishes the mode
        # this occasion was minted under, whatever the item says by then.
        render=watched_item.render,
        # Snapshotted like ``render`` (#457), and copied so a later change to
        # the item's dict can never reach the row through a shared object.
        fetch_headers=dict(watched_item.fetch_headers) if watched_item.fetch_headers else None,
        fetch_credential_headers=(
            dict(watched_item.fetch_credential_headers)
            if watched_item.fetch_credential_headers
            else None
        ),
    )
    session.add(row)
    return row


async def publish_fetch_command(
    client: Redis, row: FetchCommand, *, now: datetime | None = None
) -> None:
    """XADD the command and mark the row in-flight (caller commits).

    Publishes through ``to_wire`` over the strict Emit class — never hand-rolled
    fields (a hand-built frame dead-letters silently on the consumer side). The
    pinned User-Agent is the replicator#11 byte-continuity guarantee; the
    validators, when present, come off the row (#269) so the sweep's republish
    is byte-identical to the original command. So does the fetch-header policy
    (#457): the row's snapshot, merged here.
    """
    command = ContentFetchCommandEmit(
        occurred_at=row.issued_at,
        command_id=row.command_id,
        url=row.url,
        info_source_id=row.info_source_id,
        headers=_command_headers(row),
        render=row.render,
        credential_headers=command_credential_headers(row.fetch_credential_headers),
    )
    await AsyncBusPublisher(client).execute(BusPublish(streams.CONTENT_FETCH, to_wire(command)))
    row.status = FetchCommandStatus.IN_FLIGHT
    row.published_at = now if now is not None else datetime.now(UTC)


async def get_open_command(session: AsyncSession, watched_item_id) -> FetchCommand | None:
    """The item's open command (awaiting publish or a fact), if any.

    The scheduling gate reads this: without it, a silently failed command
    re-issues every ``schedule_tick`` — 1,440 real origin fetches a day for one
    404ing item. Returns the row rather than a bool so callers can say *how
    long* it has been open (the check-now rejection does).
    """
    stmt = (
        select(FetchCommand)
        .where(
            FetchCommand.watched_item_id == watched_item_id,
            FetchCommand.status.in_(OPEN_STATUSES),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_open_command(session: AsyncSession, watched_item_id) -> bool:
    """True when the item already has a command awaiting publish or a fact."""
    return await get_open_command(session, watched_item_id) is not None


def env_number[T: (int, float)](
    env: str, default: T, cast: Callable[[str], T], *, warn_non_positive: str = ""
) -> T:
    """One knob read, with the fallback both knobs need (CR-1, CR-3).

    An unparseable value falls back to the default rather than raising, for the
    same reason ``validator_max_age`` does: **a knob must not be able to wedge
    the path it governs**. Both of these are read while handling a failure — the
    cap on the derived leg's ``input_unreadable``, the timeout once per reaper pass
    — so a ``ValueError`` there escapes the handler, leaves the row exactly as
    it was, and the next pass repeats it. A typo would reinstate the very loop
    #275 removed.
    """
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning("unparseable %s — using the default", env, extra={"value": raw})
        return default
    if warn_non_positive and value <= 0:
        # CR-13, and CR-6's rule before it: a typo'd sign is indistinguishable
        # from a deliberate kill switch unless the effect is said out loud.
        logger.info("%s is not positive — %s", env, warn_non_positive, extra={"value": raw})
    return value


def fetch_command_timeout_seconds() -> float:
    """How long an in-flight command may go without a signal before the reaper
    expires and re-issues it.

    Deliberately generous (default 1800): Replicator's reclaim cadence is an
    operator knob on another host, and a tight value re-issues under live
    retries. Read here so the reaper and the check-now rejection quote the same
    number.
    """
    return env_number(
        FETCH_COMMAND_TIMEOUT_ENV,
        DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS,
        float,
        warn_non_positive="every in-flight command is already stale to the reaper",
    )


def fetch_max_reissues() -> int:
    """How many times one intent may be re-issued before it fails outright.

    Read here for the same reason as the timeout: two paths cap the same
    lineage counter — the reaper's stall sweep and the blob-unreadable apply
    (#275) — and they must quote the same number.
    """
    return env_number(FETCH_MAX_REISSUES_ENV, DEFAULT_FETCH_MAX_REISSUES, int)


async def select_pending_publish(session: AsyncSession, *, limit: int = 100) -> list[FetchCommand]:
    """Rows committed but never confirmed on the bus — the sweep's work list."""
    stmt = (
        select(FetchCommand)
        .where(FetchCommand.status == FetchCommandStatus.PENDING_PUBLISH)
        .order_by(FetchCommand.issued_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
