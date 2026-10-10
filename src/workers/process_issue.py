"""The content.process issue half (#325, #326): hand an applied blob to the processor.

Split from ``src/workers/process_commands.py`` so the fetch apply path can
issue without importing the derived-fact apply, which itself closes fetch rows
through ``src/workers/fetch_commands.py`` — the two worker modules would
otherwise import each other.

* ``issue_process_command`` — the decisive command. The fetch row moves to
  ``PROCESSING`` in the same commit as the command row, so no crash leaves a
  row open with nothing to close it.
* ``persist_and_publish`` — commit, then XADD; a failed publish leaves
  ``pending_publish`` for the every-minute sweep.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.bus import BUS_REDIS_URL_ENV, get_shared_bus_client
from src.core.logging import get_logger
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.models.process_command import ProcessCommand
from src.core.models.watched_item import WatchedItem
from src.core.process_commands import create_process_command, publish_process_command

logger = get_logger(__name__)


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


async def issue_process_command(
    session: AsyncSession,
    fetch_row: FetchCommand,
    watched_item: WatchedItem,
    *,
    now: datetime,
    bus_client=None,
) -> ProcessCommand:
    """Hand an applied blob to the processor to decide (#326).

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
        reissue_count=fetch_row.reissue_count,
    )
    fetch_row.status = FetchCommandStatus.PROCESSING
    await persist_and_publish(session, row, bus_client)
    return row
