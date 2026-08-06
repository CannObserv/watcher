"""Tests for the content.blobs fact consumer (#241, Phase 4 step 2).

Contract MUSTs pinned here:

* MUST-3 — correlation on ``command_id`` only; a fact for an unknown id is
  discarded (acked), never matched by URL.
* MUST-4 — at-least-once per command: a duplicate fact re-runs the upsert
  harmlessly; a late duplicate after the apply settled changes nothing.
* MUST-5 — no fingerprint dedupe: two commands returning identical bytes (same
  fingerprint, same blob_uri, different command_ids) both correlate.
"""

from datetime import UTC, datetime

import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.models.changes import BlobAvailableEmit, FetchFailedEmit

from src.core.fetch_commands import create_fetch_command
from src.core.models.fetch_command import FetchCommandStatus
from src.workers.fetch_facts import process_fact_message
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 6, 17, 0, 0, tzinfo=UTC)


def _blob_message(command_id, *, url="https://lcb.wa.gov/notices", fingerprint="ab" * 32, **over):
    event = BlobAvailableEmit(
        occurred_at=over.pop("occurred_at", NOW),
        content_fingerprint=fingerprint,
        blob_uri=over.pop("blob_uri", f"file:///var/lib/replicator/blobs/{fingerprint}.bin"),
        size_bytes=over.pop("size_bytes", 1234),
        media_type=over.pop("media_type", "text/html"),
        url=url,
        command_id=command_id,
        **over,
    )
    return from_wire(to_wire(event), topic=streams.CONTENT_BLOBS, message_id="1-1")


def _failure_message(command_id, *, terminal=True, reason="http_status", status_code=404):
    event = FetchFailedEmit(
        occurred_at=NOW,
        command_id=command_id,
        url="https://lcb.wa.gov/notices",
        reason=reason,
        terminal=terminal,
        status_code=status_code,
        detail="origin said no",
    )
    return from_wire(to_wire(event), topic=streams.CONTENT_BLOBS, message_id="1-2")


class _DeferSpy:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, command_id: str) -> None:
        self.calls.append(command_id)


async def _issued_row(db_session, **wi_kwargs):
    wi = await make_watched_item(
        db_session, primary_url=wi_kwargs.pop("primary_url", "https://lcb.wa.gov/notices")
    )
    row = await create_fetch_command(db_session, wi, now=NOW)
    row.status = FetchCommandStatus.IN_FLIGHT
    row.published_at = NOW
    await db_session.flush()
    return wi, row


class TestBlobFacts:
    async def test_blob_fact_upserts_row_and_defers_apply(self, db_session):
        _, row = await _issued_row(db_session)
        blob, fail = _DeferSpy(), _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _blob_message(
                row.command_id,
                content_type_raw="text/html; charset=utf-8",
                final_url="https://lcb.wa.gov/notices/",
                status_code=200,
                fetched_at=NOW,
            ),
            defer_blob=blob,
            defer_failure=fail,
        )

        assert outcome == "blob_recorded"
        assert row.content_fingerprint == "ab" * 32
        assert row.blob_uri.startswith("file://")
        assert row.size_bytes == 1234
        assert row.content_type_raw == "text/html; charset=utf-8"
        assert row.final_url == "https://lcb.wa.gov/notices/"
        assert row.status_code == 200
        assert row.fact_at == NOW
        assert blob.calls == [row.command_id]
        assert fail.calls == []

    async def test_duplicate_fact_is_idempotent(self, db_session):
        _, row = await _issued_row(db_session)
        blob = _DeferSpy()
        message = _blob_message(row.command_id)

        first = await process_fact_message(db_session, message, defer_blob=blob)
        second = await process_fact_message(db_session, message, defer_blob=blob)

        assert (first, second) == ("blob_recorded", "blob_recorded")
        assert blob.calls == [row.command_id, row.command_id]  # apply task guards

    async def test_late_duplicate_after_apply_changes_nothing(self, db_session):
        _, row = await _issued_row(db_session)
        row.status = FetchCommandStatus.SUCCEEDED
        row.applied_at = NOW
        await db_session.flush()
        blob = _DeferSpy()

        outcome = await process_fact_message(
            db_session, _blob_message(row.command_id), defer_blob=blob
        )

        assert outcome == "already_applied"
        assert blob.calls == []
        assert row.status == FetchCommandStatus.SUCCEEDED

    async def test_unknown_command_id_is_discarded(self, db_session):
        blob = _DeferSpy()
        outcome = await process_fact_message(
            db_session, _blob_message("01UNKNOWNCOMMANDIDXXXXXXXX"), defer_blob=blob
        )
        assert outcome == "unmatched"
        assert blob.calls == []

    async def test_same_fingerprint_two_commands_both_correlate(self, db_session):
        # MUST-5: fingerprint is content identity, command_id is correlation
        # identity — identical bytes must not suppress the second fact.
        _, row_a = await _issued_row(db_session, primary_url="https://a.example/x")
        _, row_b = await _issued_row(db_session, primary_url="https://b.example/x")
        blob = _DeferSpy()

        for row in (row_a, row_b):
            outcome = await process_fact_message(
                db_session, _blob_message(row.command_id, fingerprint="cd" * 32), defer_blob=blob
            )
            assert outcome == "blob_recorded"

        assert row_a.content_fingerprint == row_b.content_fingerprint == "cd" * 32
        assert blob.calls == [row_a.command_id, row_b.command_id]


class TestFailureFacts:
    async def test_terminal_failure_marks_row_and_defers(self, db_session):
        _, row = await _issued_row(db_session)
        blob, fail = _DeferSpy(), _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_message(row.command_id),
            defer_blob=blob,
            defer_failure=fail,
        )

        assert outcome == "failure_recorded"
        assert row.status == FetchCommandStatus.FAILED
        assert row.failure_reason == "http_status"
        assert row.failure_detail == "origin said no"
        assert row.status_code == 404
        assert fail.calls == [row.command_id]
        assert blob.calls == []

    async def test_nonterminal_failure_refreshes_fact_at_only(self, db_session):
        _, row = await _issued_row(db_session)
        fail = _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_message(row.command_id, terminal=False, reason="http_status"),
            defer_failure=fail,
        )

        assert outcome == "nonterminal_recorded"
        assert row.status == FetchCommandStatus.IN_FLIGHT
        assert row.fact_at == NOW
        assert fail.calls == []

    async def test_unknown_failure_is_discarded(self, db_session):
        fail = _DeferSpy()
        outcome = await process_fact_message(
            db_session, _failure_message("01UNKNOWNCOMMANDIDXXXXXXXX"), defer_failure=fail
        )
        assert outcome == "unmatched"
        assert fail.calls == []
