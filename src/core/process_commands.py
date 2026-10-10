"""The content.process issue path — Watcher as the processor's command issuer (#325).

Extraction runs in the cohort's processor (CannObserv/processor), never in
Watcher's process (#350), over the ``content.process`` / ``content.derived`` pair
(cannobserv#486; design ``docs/plans/2026-09-24-observo-extraction-and-diff-design.md``).
This module is the issuer half, built to ``fetch_commands``' discipline:

* **A fresh ``command_id`` per (blob, spec) occasion.** One ``source_spec`` per
  command (design D3), so the spec fallback loop is a chain of commands under
  one ``intent_id`` — ``chain_process_command`` — and a reaper re-issue is
  another — ``reissue_process_command``.
* **Persist-before-publish.** ``create_process_command`` writes the row for the
  caller to commit before any XADD; a crash in between leaves
  ``pending_publish`` for the sweep, which republishes from the row alone. So
  the row snapshots every field the wire carries.
* **The command carries the resolved dispatch essence** (cannobserv#486 D1).
  The processor's input URI ends in ``.bin``, so it cannot run the origin-URL
  tiebreaker; the issuer resolves, override applied, and states ``None`` when
  nothing is informative (the HTML fallback).
* **``input_digest`` is the blob fact's bare hex.** The Emit refuses a
  ``sha256:``-prefixed value, and the refusal is taken at the occasion
  (``UnsendableProcessCommand``) rather than at publish, where an unbuildable
  row would fail the sweep every minute forever.

Every applied blob is issued, and the processor's answer decides (#326).
"""

from datetime import UTC, datetime

from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import to_wire
from co_core.pure.models.changes import ContentProcessCommandEmit
from co_core_aio.bus import AsyncBusPublisher
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.fetch_commands import env_number
from src.core.logging import get_logger
from src.core.media_type import resolve_dispatch_essence
from src.core.models.fetch_command import FetchCommand
from src.core.models.process_command import ProcessCommand, ProcessCommandStatus
from src.core.models.watched_item import WatchedItem

logger = get_logger(__name__)

# The only processor Watcher asks for: the transform a change fingerprint covers.
PROCESSOR = "extract"

# The one terminal reason that says the *input* is gone, not that the bytes
# were judged: the raw blob expired or was never readable. Processor-decided,
# it re-fetches under the #275 cap (#326); every other terminal reason is the
# extraction-failure path.
INPUT_UNREADABLE_REASON = "input_unreadable"

PROCESS_COMMAND_TIMEOUT_ENV = "WATCHER_PROCESS_COMMAND_TIMEOUT_SECONDS"
DEFAULT_PROCESS_COMMAND_TIMEOUT_SECONDS = 1800.0
PROCESS_COMMAND_HARD_LIMIT_ENV = "WATCHER_PROCESS_COMMAND_HARD_LIMIT_SECONDS"
DEFAULT_PROCESS_COMMAND_HARD_LIMIT_SECONDS = 86400.0


def process_command_timeout_seconds() -> float:
    """How long an in-flight command may go without a signal before the reaper
    may re-issue it — **only while the processor is consuming** (#325).

    A command sitting in the processor's group is not lost; re-issuing it during
    an outage only puts a duplicate in a stream that is never trimmed.
    """
    return env_number(
        PROCESS_COMMAND_TIMEOUT_ENV,
        DEFAULT_PROCESS_COMMAND_TIMEOUT_SECONDS,
        float,
        warn_non_positive="every in-flight process command is already stale to the reaper",
    )


def process_command_hard_limit_seconds() -> float:
    """How long a command may stay in flight, consuming or not, before the
    reaper gives up on it (#325, the processor#17 residual).

    The no-re-issue rule reads "the processor is consuming" from facts for
    *other* commands. A command whose failure fact was refused gets no fact at
    all, and in a quiet period nothing else arrives either — so without a hard
    ceiling it would wait forever.
    """
    return env_number(
        PROCESS_COMMAND_HARD_LIMIT_ENV,
        DEFAULT_PROCESS_COMMAND_HARD_LIMIT_SECONDS,
        float,
        warn_non_positive="every in-flight process command is already past the hard limit",
    )


class UnsendableProcessCommand(ValueError):
    """The occasion cannot produce a command the contract accepts."""


def _command_emit(row: ProcessCommand) -> ContentProcessCommandEmit:
    """The wire command for ``row`` — through the strict Emit class, never hand-rolled."""
    return ContentProcessCommandEmit(
        occurred_at=row.issued_at,
        command_id=row.command_id,
        info_source_id=row.info_source_id,
        input_uri=row.input_uri,
        input_digest=row.input_digest,
        processor=PROCESSOR,
        source_spec=row.source_spec,
        media_type=row.media_type,
    )


def _checked(row: ProcessCommand) -> ProcessCommand:
    """Refuse a row the Emit cannot build, before anything persists it."""
    try:
        _command_emit(row)
    except ValidationError as exc:
        raise UnsendableProcessCommand(str(exc)) from exc
    return row


async def create_process_command(
    session: AsyncSession,
    fetch_row: FetchCommand,
    watched_item: WatchedItem,
    *,
    now: datetime,
    reissue_count: int = 0,
) -> ProcessCommand:
    """Persist the spec[0] command for a fetch occasion (caller commits before publishing).

    A new intent. The essence is resolved **here**, from the item as the apply
    path left it (the media type is seeded from this occasion's header first).

    ``reissue_count`` seeds the lineage's counter: a decisive lineage spans
    both legs, so it starts where the fetch leg's left off and the shared
    ``WATCHER_FETCH_MAX_REISSUES`` caps the whole of it.

    Raises ``UnsendableProcessCommand`` when the occasion has nothing to send —
    no spec, no blob — or the blob fact's digest is not the bare hex the
    contract requires.
    """
    specs = watched_item.source_specs or []
    if not specs:
        raise UnsendableProcessCommand("watched item has no source_specs")
    if not fetch_row.blob_uri or not fetch_row.content_fingerprint:
        raise UnsendableProcessCommand("fetch command holds no blob fact")
    row = _checked(
        ProcessCommand(
            command_id=str(ULID()),
            intent_id=str(ULID()),
            fetch_command_id=fetch_row.command_id,
            watched_item_id=watched_item.id,
            info_source_id=watched_item.archiver_info_source_id,
            input_uri=fetch_row.blob_uri,
            input_digest=fetch_row.content_fingerprint,
            spec_index=0,
            source_spec=specs[0],
            media_type=resolve_dispatch_essence(
                watched_item.content_media_type, watched_item.effective_url
            ),
            status=ProcessCommandStatus.PENDING_PUBLISH,
            issued_at=now,
            reissue_count=reissue_count,
        )
    )
    session.add(row)
    return row


def _successor(
    prior: ProcessCommand, *, now: datetime, spec_index: int, source_spec: dict, reissue_count: int
) -> ProcessCommand:
    """A fresh command for ``prior``'s occasion: same lineage, same snapshot."""
    return _checked(
        ProcessCommand(
            command_id=str(ULID()),
            intent_id=prior.intent_id,
            fetch_command_id=prior.fetch_command_id,
            watched_item_id=prior.watched_item_id,
            info_source_id=prior.info_source_id,
            input_uri=prior.input_uri,
            input_digest=prior.input_digest,
            spec_index=spec_index,
            source_spec=source_spec,
            media_type=prior.media_type,
            status=ProcessCommandStatus.PENDING_PUBLISH,
            issued_at=now,
            reissue_count=reissue_count,
        )
    )


async def chain_process_command(
    session: AsyncSession, prior: ProcessCommand, source_spec: dict, *, now: datetime
) -> ProcessCommand:
    """The next spec in the fallback loop, after ``prior`` came back empty (D3).

    Same intent and the same occasion snapshot. Not a re-issue: the lineage's
    re-issue count carries over unchanged.
    """
    row = _successor(
        prior,
        now=now,
        spec_index=prior.spec_index + 1,
        source_spec=source_spec,
        reissue_count=prior.reissue_count,
    )
    session.add(row)
    return row


async def reissue_process_command(
    session: AsyncSession, prior: ProcessCommand, *, now: datetime
) -> ProcessCommand:
    """``prior`` again under a fresh ``command_id``: same spec, ``reissue_count + 1``."""
    row = _successor(
        prior,
        now=now,
        spec_index=prior.spec_index,
        source_spec=prior.source_spec,
        reissue_count=prior.reissue_count + 1,
    )
    session.add(row)
    return row


async def publish_process_command(
    client: Redis, row: ProcessCommand, *, now: datetime | None = None
) -> None:
    """XADD the command and mark the row in-flight (caller commits).

    No ``maxlen``: ``content.process`` is never trimmed (broker#62) — a cap on a
    command stream deletes commands the processor's group has not been
    delivered.
    """
    await AsyncBusPublisher(client).execute(
        BusPublish(streams.CONTENT_PROCESS, to_wire(_command_emit(row)))
    )
    row.status = ProcessCommandStatus.IN_FLIGHT
    row.published_at = now if now is not None else datetime.now(UTC)


async def select_pending_process_publish(
    session: AsyncSession, *, limit: int = 100
) -> list[ProcessCommand]:
    """Rows committed but never confirmed on the bus — the sweep's work list."""
    stmt = (
        select(ProcessCommand)
        .where(ProcessCommand.status == ProcessCommandStatus.PENDING_PUBLISH)
        .order_by(ProcessCommand.issued_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
