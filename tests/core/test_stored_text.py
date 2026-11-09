"""Where the processor stored a canonical text (#345, #369).

The pipeline puts the previous text's location on the ``change_detected``
fact Archiver notifies from. The processor's own answer says where the text
lives: ``process_commands.output_uri`` on a completed row whose
``output_digest`` is the fingerprint. Moved out of the deleted notification
subsystem's diff loader by #379, which left it as the only reader.
"""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from src.core.fetch_commands import create_fetch_command
from src.core.models.process_command import ProcessCommandStatus
from src.core.models.watched_item import WatchedItem
from src.core.process_commands import create_process_command
from src.core.stored_text import StoredText, stored_text_location
from tests.conftest import make_watched_item


def _fp(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _uri(fingerprint: str) -> str:
    return f"gs://co-gcs-processor/blobs/{fingerprint.removeprefix('sha256:')}.bin"


@pytest.mark.integration
class TestStoredTextLocation:
    """The processor's own answer says where the text lives (``output_uri``)."""

    async def _row(self, db_session, *, status, digest, uri, size=12):
        wi = await make_watched_item(db_session, source_specs=[{"selector": "main"}])
        fetch = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        fetch.blob_uri = f"gs://co-gcs-blobs/blobs/{'61' * 32}.bin"
        fetch.content_fingerprint = "61" * 32
        await db_session.flush()
        row = await create_process_command(db_session, fetch, wi, now=datetime.now(UTC))
        row.status = status
        row.output_digest = digest
        row.output_uri = uri
        row.output_size_bytes = size
        await db_session.flush()
        return row

    async def test_a_completed_row_names_the_text(self, db_session):
        fp = _fp(b"stored")
        await self._row(db_session, status=ProcessCommandStatus.COMPLETED, digest=fp, uri=_uri(fp))
        assert await stored_text_location(db_session, fp) == StoredText(uri=_uri(fp), size_bytes=12)

    async def test_an_unanswered_row_is_not_a_location(self, db_session):
        fp = _fp(b"in flight")
        await self._row(db_session, status=ProcessCommandStatus.IN_FLIGHT, digest=fp, uri=None)
        assert await stored_text_location(db_session, fp) is None

    async def test_no_row_is_none(self, db_session):
        assert await stored_text_location(db_session, _fp(b"never stored")) is None

    async def test_a_database_error_leaves_the_callers_transaction_usable(self, db_session):
        """The lookup shares the pipeline's open transaction. A server-side
        error there (a NUL is not storable text) rolls back to the savepoint
        only: the rows the caller wrote before it survive, in a transaction
        it can still use."""
        wi = await make_watched_item(db_session, source_specs=[{"selector": "main"}])
        with pytest.raises(DBAPIError):
            await stored_text_location(db_session, "sha256:\x00")
        stmt = select(WatchedItem.id).where(WatchedItem.id == wi.id)
        assert (await db_session.execute(stmt)).scalar_one() == wi.id
