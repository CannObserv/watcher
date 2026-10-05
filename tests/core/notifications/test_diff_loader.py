"""Tests for loading the change diff (#222): locate, verify, cap — never raise.

The processor stores the canonical text under its own digest, so a revision's
``content_fingerprint`` is the address of the text it hashed. The loader reads
both sides, checks each hashes to its fingerprint, and hands ``difflib`` to a
thread. Every failure is a ``ChangeDiff`` carrying the reason, never an
exception into dispatch.
"""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.core.blobs import BlobUnreadable, UnsupportedBlobScheme
from src.core.fetch_commands import create_fetch_command
from src.core.models.process_command import LocalOutcome, ProcessCommandStatus
from src.core.models.watched_item import WatchedItem
from src.core.notifications import diff_loader as loader_mod
from src.core.notifications.diff import ChangeDiff, compute_unified_diff
from src.core.notifications.diff_loader import (
    DIFF_MAX_INPUT_BYTES_ENV,
    StoredText,
    load_change_diff,
    stored_text_location,
)
from src.core.process_commands import LocalExtraction, create_process_command
from tests.conftest import make_watched_item

PREVIOUS = b"Hours\nMon-Fri 9-5\nContact: a@example.com"
CURRENT = b"Hours\nMon-Fri 9-6\nContact: a@example.com"


def _fp(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _meta(previous: bytes = PREVIOUS, current: bytes = CURRENT) -> dict:
    return {"previous_fingerprint": _fp(previous), "current_fingerprint": _fp(current)}


def _uri(fingerprint: str) -> str:
    return f"gs://co-gcs-processor/blobs/{fingerprint.removeprefix('sha256:')}.bin"


def _store(*texts: bytes):
    """Patch the location lookup and the read to serve ``texts`` by digest."""
    by_uri = {_uri(_fp(t)): t for t in texts}

    async def locate(_session, fingerprint):
        uri = _uri(fingerprint)
        return StoredText(uri=uri, size_bytes=len(by_uri[uri])) if uri in by_uri else None

    async def read(uri):
        return by_uri[uri]

    return (
        patch.object(loader_mod, "stored_text_location", side_effect=locate),
        patch.object(loader_mod, "aread_blob", side_effect=read),
    )


class TestLoadChangeDiff:
    async def test_none_when_the_event_names_no_fingerprints(self):
        assert await load_change_diff(AsyncMock(), {}) is None
        assert await load_change_diff(AsyncMock(), {"current_fingerprint": _fp(CURRENT)}) is None

    async def test_diffs_two_stored_texts(self):
        locate, read = _store(PREVIOUS, CURRENT)
        with locate, read:
            result = await load_change_diff(AsyncMock(), _meta())
        assert result == ChangeDiff(unified=compute_unified_diff(PREVIOUS, CURRENT))

    async def test_current_text_in_hand_is_used_without_a_lookup(self):
        # Local/shadow extraction notifies before the processor has answered,
        # so the current text is not stored yet — but it is in memory.
        locate, read = _store(PREVIOUS)
        with locate as located, read:
            result = await load_change_diff(AsyncMock(), _meta(), current_text=CURRENT)
        assert result.unified == compute_unified_diff(PREVIOUS, CURRENT)
        assert [c.args[1] for c in located.call_args_list] == [_fp(PREVIOUS)]

    async def test_current_text_in_hand_that_does_not_hash_is_not_trusted(self):
        locate, read = _store(PREVIOUS, CURRENT)
        with locate, read:
            result = await load_change_diff(AsyncMock(), _meta(), current_text=b"something else")
        assert result.unified == compute_unified_diff(PREVIOUS, CURRENT)

    async def test_previous_not_stored_is_unavailable(self):
        locate, read = _store(CURRENT)
        with locate, read:
            result = await load_change_diff(AsyncMock(), _meta())
        assert result.unified == ""
        assert result.unavailable == "previous text not stored"

    async def test_current_not_stored_is_unavailable(self):
        locate, read = _store(PREVIOUS)
        with locate, read:
            result = await load_change_diff(AsyncMock(), _meta())
        assert result.unavailable == "current text not stored"

    async def test_corrupt_stored_text_is_unavailable_and_warned(self, caplog):
        meta = _meta()
        by_uri = {_uri(meta["previous_fingerprint"]): b"tampered", _uri(_fp(CURRENT)): CURRENT}

        async def locate(_s, fp):
            return StoredText(uri=_uri(fp), size_bytes=8)

        with (
            patch.object(loader_mod, "stored_text_location", side_effect=locate),
            patch.object(loader_mod, "aread_blob", side_effect=lambda uri: by_uri[uri]),
        ):
            result = await load_change_diff(AsyncMock(), meta)
        assert result.unavailable == "previous text failed its hash check"
        assert any("hash check" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        ("setup", "message"),
        [
            ("corrupt", "stored text failed its hash check — no diff"),
            ("unreadable", "stored text unreadable — no diff"),
            ("missing", "change diff unavailable"),
            ("broken", "change diff failed — sending without it"),
        ],
    )
    async def test_every_log_line_names_the_item(self, caplog, setup, message):
        # An operator holding a WARNING must be able to find the item without
        # reverse-searching revisions by digest (CR 9).
        caplog.set_level(logging.INFO)

        async def locate(_s, fp):
            if setup == "broken":
                raise RuntimeError("db gone")
            if setup == "missing":
                return None
            return StoredText(uri=_uri(fp), size_bytes=8)

        def read(_uri):
            if setup == "unreadable":
                raise BlobUnreadable("gone")
            return b"tampered"

        with (
            patch.object(loader_mod, "stored_text_location", side_effect=locate),
            patch.object(loader_mod, "aread_blob", side_effect=read),
        ):
            await load_change_diff(AsyncMock(), _meta(), watched_item_id="01ITEM")
        (record,) = [r for r in caplog.records if r.getMessage() == message]
        assert record.watched_item_id == "01ITEM"

    @pytest.mark.parametrize("error", [BlobUnreadable("gone"), UnsupportedBlobScheme("403")])
    async def test_an_unreadable_text_is_unavailable(self, error):
        async def locate(_s, fp):
            return StoredText(uri=_uri(fp), size_bytes=10)

        with (
            patch.object(loader_mod, "stored_text_location", side_effect=locate),
            patch.object(loader_mod, "aread_blob", side_effect=error),
        ):
            result = await load_change_diff(AsyncMock(), _meta())
        assert result.unavailable == "previous text unreadable"

    async def test_anything_unexpected_is_unavailable_never_raised(self):
        with patch.object(loader_mod, "stored_text_location", side_effect=RuntimeError("db gone")):
            result = await load_change_diff(AsyncMock(), _meta())
        assert result.unavailable == "error"

    async def test_too_large_by_its_recorded_size_is_never_read(self, monkeypatch):
        monkeypatch.setenv(DIFF_MAX_INPUT_BYTES_ENV, "16")

        async def locate(_s, fp):
            return StoredText(uri=_uri(fp), size_bytes=17)

        read = AsyncMock()
        with (
            patch.object(loader_mod, "stored_text_location", side_effect=locate),
            patch.object(loader_mod, "aread_blob", read),
        ):
            result = await load_change_diff(AsyncMock(), _meta())
        assert result.unavailable == "content too large"
        read.assert_not_awaited()

    async def test_too_large_in_hand_is_unavailable(self, monkeypatch):
        monkeypatch.setenv(DIFF_MAX_INPUT_BYTES_ENV, str(len(PREVIOUS)))
        big = CURRENT + b"\n" + b"x" * 64
        locate, read = _store(PREVIOUS)
        with locate, read:
            result = await load_change_diff(AsyncMock(), _meta(current=big), current_text=big)
        assert result.unavailable == "content too large"

    async def test_oversized_text_in_hand_is_refused_before_it_is_hashed(self, monkeypatch):
        # Hashing is CPU on the event loop; a text the cap refuses anyway
        # must not pay for it.
        monkeypatch.setenv(DIFF_MAX_INPUT_BYTES_ENV, str(len(PREVIOUS)))
        big = CURRENT + b"\n" + b"x" * 64
        hashed = []
        real = loader_mod._address
        locate, read = _store(PREVIOUS)
        with (
            locate,
            read,
            patch.object(loader_mod, "_address", side_effect=lambda t: hashed.append(t) or real(t)),
        ):
            result = await load_change_diff(AsyncMock(), _meta(current=big), current_text=big)
        assert result.unavailable == "content too large"
        assert big not in hashed

    async def test_difflib_runs_off_the_event_loop(self):
        locate, read = _store(PREVIOUS, CURRENT)
        real = asyncio.to_thread
        calls = []

        async def spy(fn, *args, **kwargs):
            calls.append(fn)
            return await real(fn, *args, **kwargs)

        with locate, read, patch.object(loader_mod.asyncio, "to_thread", side_effect=spy):
            await load_change_diff(AsyncMock(), _meta())
        assert compute_unified_diff in calls


@pytest.mark.integration
class TestStoredTextLocation:
    """The processor's own answer says where the text lives (``output_uri``)."""

    async def _row(self, db_session, *, status, digest, uri, size=12):
        wi = await make_watched_item(db_session, source_specs=[{"selector": "main"}])
        fetch = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        fetch.blob_uri = f"gs://co-gcs-blobs/blobs/{'61' * 32}.bin"
        fetch.content_fingerprint = "61" * 32
        await db_session.flush()
        row = await create_process_command(
            db_session,
            fetch,
            wi,
            now=datetime.now(UTC),
            local=LocalExtraction(outcome=LocalOutcome.CHANGED, fingerprint=digest),
        )
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
        error there (a NUL is not storable text) must cost the diff, never the
        revision and audit rows the caller still has to write and commit."""
        wi = await make_watched_item(db_session, source_specs=[{"selector": "main"}])
        result = await load_change_diff(
            db_session,
            {"previous_fingerprint": "sha256:\x00", "current_fingerprint": _fp(CURRENT)},
            current_text=CURRENT,
        )
        assert result.unavailable == "error"
        # The item written before the error is still there, in a usable transaction.
        stmt = select(WatchedItem.id).where(WatchedItem.id == wi.id)
        assert (await db_session.execute(stmt)).scalar_one() == wi.id
