"""The content.process issue half (#325, #326): send an applied blob to the processor.

Split from ``src/workers/process_commands.py`` so the fetch apply path can
issue without importing the derived-fact apply, which itself closes fetch rows
through ``src/workers/fetch_commands.py`` — the two worker modules would
otherwise import each other.

* ``issue_process_command`` — **processor mode**: the decisive command. The
  fetch row moves to ``PROCESSING`` in the same commit as the command row, so
  no crash leaves a row open with nothing to close it.
* ``issue_shadow_process_command`` — **shadow mode**: a side lineage beside
  local's committed answer; never raises into the apply.
* ``persist_and_publish`` — commit, then XADD; a failed publish leaves
  ``pending_publish`` for the every-minute sweep.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.bus import BUS_REDIS_URL_ENV, get_shared_bus_client
from src.core.extract_mode import ExtractMode, extract_mode
from src.core.logging import get_logger
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.models.process_command import LocalOutcome, ProcessCommand
from src.core.models.watched_item import WatchedItem
from src.core.process_commands import (
    LocalExtraction,
    UnsendableProcessCommand,
    create_process_command,
    publish_process_command,
)
from src.workers.pipeline import WatchedItemResult

logger = get_logger(__name__)


def local_extraction_of(result: WatchedItemResult) -> LocalExtraction:
    """Local extraction's answer, as the pipeline reported it."""
    if result.baseline_established:
        outcome = LocalOutcome.BASELINE
    elif result.changed or result.rebaselined:
        # The comparator judges digests, not policy: a re-baseline is a moved
        # fingerprint like any other (#326).
        outcome = LocalOutcome.CHANGED
    else:
        outcome = LocalOutcome.UNCHANGED
    return LocalExtraction(
        outcome=outcome,
        fingerprint=result.content_fingerprint,
        spec_fingerprint=result.spec_fingerprint,
    )


async def persist_and_publish(session: AsyncSession, row: ProcessCommand, client) -> None:
    """Commit the row, then XADD it; a failed publish leaves it for the sweep."""
    await session.commit()
    publish_client = client if client is not None else get_shared_bus_client()
    if publish_client is None:
        logger.error(
            "process command cannot publish: %s is not set",
            BUS_REDIS_URL_ENV,
            extra={"command_id": row.command_id},
        )
        return
    try:
        await publish_process_command(publish_client, row)
        await session.commit()
    except Exception:
        logger.warning(
            "process command publish failed; the sweep will retry it",
            extra={"command_id": row.command_id},
            exc_info=True,
        )


async def issue_shadow_process_command(
    session: AsyncSession,
    fetch_row: FetchCommand,
    watched_item: WatchedItem,
    local: LocalExtraction,
    *,
    bus_client=None,
) -> str | None:
    """Send an applied blob to the processor beside local's answer (shadow mode only).

    Called after the apply has committed its own outcome, so a failure here
    loses one comparison and nothing else: every exception is logged and
    swallowed, and the session rolled back to the apply's committed state.
    Returns the new ``command_id``, or ``None`` when nothing was issued.
    """
    if extract_mode() is not ExtractMode.SHADOW:
        return None
    if not watched_item.source_specs:
        # Nothing to send, and local already reports the item ERROR every cycle
        # (#260): a WARNING here too would only repeat it (CR 7).
        logger.debug(
            "watched item has no source_specs — not shadowing the occasion",
            extra={"fetch_command_id": fetch_row.command_id},
        )
        return None
    if local.outcome is not LocalOutcome.EXTRACTION_FAILED and local.fingerprint is None:
        # Reading a missing fingerprint as "local failed" would manufacture a
        # mismatch; with nothing to compare against, there is nothing to send.
        logger.warning(
            "local result carries no fingerprint — not shadowing the occasion",
            extra={"fetch_command_id": fetch_row.command_id},
        )
        return None
    try:
        row = await create_process_command(
            session, fetch_row, watched_item, now=datetime.now(UTC), local=local
        )
        await persist_and_publish(session, row, bus_client)
        return row.command_id
    except UnsendableProcessCommand as exc:
        logger.warning(
            "occasion is unsendable to the processor — not shadowing it",
            extra={"fetch_command_id": fetch_row.command_id, "error": str(exc)},
        )
    except Exception:
        logger.warning(
            "shadow process command failed — local outcome stands",
            extra={"fetch_command_id": fetch_row.command_id},
            exc_info=True,
        )
    await session.rollback()
    return None


async def issue_process_command(
    session: AsyncSession,
    fetch_row: FetchCommand,
    watched_item: WatchedItem,
    *,
    now: datetime,
    bus_client=None,
) -> ProcessCommand:
    """Hand an applied blob to the processor to decide (processor mode, #326).

    The fetch row moves to ``PROCESSING`` — still open, so the scheduling gate
    holds — and the spec[0] command is persisted beside it: **one commit**, so
    an open row always has a command that will close it. The lineage starts at
    the fetch row's re-issue count; it spans both legs under one cap.

    Raises ``UnsendableProcessCommand`` before anything is written when the
    occasion has nothing to send (no spec, no blob, a prefixed digest); the
    caller closes the row as an extraction failure.
    """
    row = await create_process_command(
        session,
        fetch_row,
        watched_item,
        now=now,
        local=None,
        reissue_count=fetch_row.reissue_count,
    )
    fetch_row.status = FetchCommandStatus.PROCESSING
    await persist_and_publish(session, row, bus_client)
    return row
