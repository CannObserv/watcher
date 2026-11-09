"""Where the processor stored a canonical text (#345, #369).

The processor stores the canonical text permanently, content-addressed, so a
fingerprint *is* the address of the text it names. **Where** it lives comes
from the processor's own answer: ``process_commands.output_uri`` on a
completed row whose ``output_digest`` is the fingerprint. No bucket is
configured on this side, and a fingerprint the processor never answered for
(a revision older than shadow mode, #325) has no location.

The pipeline puts the previous text's location on the ``change_detected``
fact Archiver notifies from (#369). Watcher never reads the text itself: the
change diff that did went with the notification subsystem (#379).
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.process_command import ProcessCommand, ProcessCommandStatus


@dataclass(frozen=True)
class StoredText:
    """Where the processor stored one canonical text, and its recorded size."""

    uri: str
    size_bytes: int | None


async def stored_text_location(session: AsyncSession, fingerprint: str) -> StoredText | None:
    """The processor's stored copy of the text ``fingerprint`` names, or ``None``.

    Any completed answer will do: the store is content-addressed and
    write-if-absent, so every row reporting this digest names the same bytes.

    **Under a savepoint** (#222 CR 1): the session is the pipeline's, mid-apply.
    A server-side error would otherwise abort its whole transaction, and the
    revision and audit rows the caller has yet to commit would go with it; the
    rollback stops at the savepoint, and the error still reaches the caller.
    """
    async with session.begin_nested():
        row = (
            await session.execute(
                select(ProcessCommand.output_uri, ProcessCommand.output_size_bytes)
                .where(
                    ProcessCommand.output_digest == fingerprint,
                    ProcessCommand.status == ProcessCommandStatus.COMPLETED,
                    ProcessCommand.output_uri.is_not(None),
                )
                .order_by(ProcessCommand.fact_at.desc().nulls_last())
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    return StoredText(uri=row.output_uri, size_bytes=row.output_size_bytes)
