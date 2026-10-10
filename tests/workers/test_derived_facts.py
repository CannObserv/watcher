"""Tests for the content.derived fact consumer (#325).

What the consumer owes the process leg:

* correlation on ``command_id`` only; a fact for a command Watcher did not issue
  is discarded — ``content.derived`` is broadcast;
* **the first terminal fact wins** (CannObserv/processor#17): a lost ack makes
  the processor publish the same outcome again under a fresh ``occurred_at``,
  which is a distinct envelope key, and a give-up can follow a success it could
  not ack. Neither may reach the apply twice or overwrite the settled answer;
* a non-terminal failure only refreshes ``fact_at`` — the reaper's signal that
  the processor is alive.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.adapters.bus.streams import group_name
from co_core.pure.extract import CANONICAL_TEXT_MEDIA_TYPE
from co_core.pure.models.changes import ProcessingCompleteEmit, ProcessingFailedEmit
from co_core_aio.bus import AsyncBusPublisher
from sqlalchemy import event

import src.workers.derived_facts as df_mod
from src.core.fetch_commands import create_fetch_command
from src.core.models.process_command import ProcessCommandStatus
from src.core.process_commands import create_process_command
from src.workers.derived_facts import (
    CONSUMER_GROUP,
    process_derived_message,
    run_derived_consumer,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 2, 17, 0, 0, tzinfo=UTC)
DIGEST = "sha256:" + "ab" * 32
FOREIGN_INFO_SOURCE_ID = "01FOREIGNINFOSOURCEIDXXXXX"


def _complete(row_or_id, *, occurred_at=NOW, **over):
    command_id = getattr(row_or_id, "command_id", row_or_id)
    info_source_id = getattr(row_or_id, "info_source_id", FOREIGN_INFO_SOURCE_ID)
    fields = {
        "occurred_at": occurred_at,
        "command_id": command_id,
        "info_source_id": info_source_id,
        "empty": False,
        "output_digest": DIGEST,
        "output_uri": f"gs://co-gcs-processor/blobs/{'ab' * 32}.bin",
        "output_size_bytes": 42,
        "output_media_type": CANONICAL_TEXT_MEDIA_TYPE,
        "spec_schema_version": 1,
        "processor_version": "0.19.7+1",
        "spec_fingerprint": "spec1:sha256:" + "cd" * 32,
        **over,
    }
    event = ProcessingCompleteEmit(**fields)
    return from_wire(to_wire(event), topic=streams.CONTENT_DERIVED, message_id="1-1")


def _failed(row, *, reason="extraction_error", terminal=True, occurred_at=NOW, **over):
    event = ProcessingFailedEmit(
        occurred_at=occurred_at,
        command_id=row.command_id,
        info_source_id=over.pop("info_source_id", row.info_source_id),
        reason=reason,
        terminal=terminal,
        detail=over.pop("detail", "parser raised"),
    )
    return from_wire(to_wire(event), topic=streams.CONTENT_DERIVED, message_id="1-2")


class _DeferSpy:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, command_id: str) -> None:
        self.calls.append(command_id)


async def _issued(db_session, *, status=ProcessCommandStatus.IN_FLIGHT):
    wi = await make_watched_item(
        db_session,
        primary_url="https://lcb.wa.gov/boardmeetings",
        source_specs=[{"extraction": {"algorithm": "full_page"}, "schema_version": 1}],
    )
    fetch = await create_fetch_command(db_session, wi, now=NOW)
    fetch.content_fingerprint = "61" * 32
    fetch.blob_uri = f"gs://co-gcs-blobs/blobs/{'61' * 32}.bin"
    await db_session.flush()
    row = await create_process_command(db_session, fetch, wi, now=NOW)
    row.status = status
    await db_session.flush()
    return row


class TestCompleteFacts:
    async def test_records_the_fact_and_defers_the_apply(self, db_session):
        row = await _issued(db_session)
        defer = _DeferSpy()

        outcome = await process_derived_message(db_session, _complete(row), defer=defer)

        assert outcome == "complete_recorded"
        assert row.status == ProcessCommandStatus.COMPLETED
        assert row.fact_at == NOW
        assert row.empty is False
        assert row.output_digest == DIGEST
        assert row.output_uri.startswith("gs://co-gcs-processor/")
        assert row.output_size_bytes == 42
        assert row.output_media_type == CANONICAL_TEXT_MEDIA_TYPE
        assert row.spec_schema_version == 1
        assert row.processor_version == "0.19.7+1"
        assert row.spec_fingerprint.startswith("spec1:")
        assert defer.calls == [row.command_id]

    async def test_empty_outcome_is_recorded_as_a_result(self, db_session):
        row = await _issued(db_session)
        defer = _DeferSpy()
        fact = _complete(row, empty=True, output_digest=None, output_uri=None, output_size_bytes=0)

        assert await process_derived_message(db_session, fact, defer=defer) == "complete_recorded"
        assert row.status == ProcessCommandStatus.COMPLETED
        assert row.empty is True
        assert row.output_digest is None
        assert defer.calls == [row.command_id]

    async def test_a_fact_racing_the_publish_commit_still_correlates(self, db_session):
        # The XADD succeeded and the IN_FLIGHT commit did not: the row is still
        # pending_publish when the answer arrives. It is ours; take it.
        row = await _issued(db_session, status=ProcessCommandStatus.PENDING_PUBLISH)
        defer = _DeferSpy()

        assert await process_derived_message(db_session, _complete(row), defer=defer) == (
            "complete_recorded"
        )
        assert row.status == ProcessCommandStatus.COMPLETED


class TestFirstTerminalFactWins:
    async def test_a_repeated_success_is_discarded(self, db_session, caplog):
        caplog.set_level(logging.INFO)
        row = await _issued(db_session)
        defer = _DeferSpy()
        await process_derived_message(db_session, _complete(row), defer=defer)

        later = _complete(
            row, occurred_at=NOW + timedelta(seconds=30), processor_version="0.19.7+2"
        )
        outcome = await process_derived_message(db_session, later, defer=defer)

        assert outcome == "already_settled"
        assert row.fact_at == NOW
        assert any(getattr(r, "occurred_at", "").endswith("Z") for r in caplog.records)  # CR 6
        assert row.processor_version == "0.19.7+1"
        assert defer.calls == [row.command_id]  # one apply, not two

    async def test_a_failure_after_a_success_does_not_overwrite_it(self, db_session):
        row = await _issued(db_session)
        defer = _DeferSpy()
        await process_derived_message(db_session, _complete(row), defer=defer)

        give_up = _failed(
            row,
            occurred_at=NOW + timedelta(minutes=1),
            detail="dead-lettered: gave up on attempt 3: ack refused",
        )
        outcome = await process_derived_message(db_session, give_up, defer=defer)

        assert outcome == "already_settled"
        assert row.status == ProcessCommandStatus.COMPLETED
        assert row.failure_reason is None
        assert defer.calls == [row.command_id]

    async def test_a_late_fact_for_an_expired_command_is_discarded(self, db_session):
        row = await _issued(db_session, status=ProcessCommandStatus.EXPIRED)
        defer = _DeferSpy()

        assert await process_derived_message(db_session, _complete(row), defer=defer) == "late"
        assert row.status == ProcessCommandStatus.EXPIRED
        assert row.output_digest is None
        assert defer.calls == []


class TestFailureFacts:
    async def test_terminal_failure_settles_the_row(self, db_session):
        row = await _issued(db_session)
        defer = _DeferSpy()

        outcome = await process_derived_message(
            db_session, _failed(row, reason="input_unreadable", detail="past the TTL"), defer=defer
        )

        assert outcome == "failure_recorded"
        assert row.status == ProcessCommandStatus.FAILED
        assert row.failure_reason == "input_unreadable"
        assert row.failure_detail == "past the TTL"
        assert row.fact_at == NOW
        assert defer.calls == [row.command_id]

    async def test_nonterminal_failure_only_refreshes_the_signal(self, db_session):
        row = await _issued(db_session)
        defer = _DeferSpy()
        later = NOW + timedelta(minutes=5)

        outcome = await process_derived_message(
            db_session,
            _failed(row, reason="transient", terminal=False, occurred_at=later),
            defer=defer,
        )

        assert outcome == "nonterminal_recorded"
        assert row.status == ProcessCommandStatus.IN_FLIGHT
        assert row.fact_at == later
        assert row.failure_reason is None
        assert defer.calls == []


class TestCorrelation:
    async def test_a_fact_for_a_command_we_did_not_issue_is_discarded(self, db_session, caplog):
        defer = _DeferSpy()
        with caplog.at_level(logging.INFO):
            outcome = await process_derived_message(
                db_session, _complete("01NOTOURSCOMMANDIDXXXXXXXX"), defer=defer
            )
        assert outcome == "unmatched"
        assert defer.calls == []
        assert any(
            getattr(r, "command_id", None) == "01NOTOURSCOMMANDIDXXXXXXXX" for r in caplog.records
        )

    async def test_echo_mismatch_is_reported_but_correlation_stands(self, db_session, caplog):
        row = await _issued(db_session)
        defer = _DeferSpy()
        fact = _failed(row, info_source_id=FOREIGN_INFO_SOURCE_ID)

        with caplog.at_level(logging.WARNING):
            outcome = await process_derived_message(db_session, fact, defer=defer)

        assert outcome == "failure_recorded"
        assert "info_source_id echo mismatch" in caplog.text


class TestRunDerivedConsumer:
    def test_group_is_derived(self):
        assert CONSUMER_GROUP == group_name(streams.CONTENT_DERIVED, "watcher") == "watcher.derived"

    async def test_reads_settles_and_acks(self, db_session, monkeypatch):
        row = await _issued(db_session)
        await db_session.commit()
        defer = _DeferSpy()
        monkeypatch.setattr(df_mod, "_defer_apply", defer)
        client = fakeredis.FakeAsyncRedis()
        stop = asyncio.Event()

        @asynccontextmanager
        async def _ctx():
            yield db_session

        task = asyncio.create_task(
            run_derived_consumer(client, lambda: _ctx(), stop=stop, block_ms=10)
        )
        await asyncio.sleep(0.1)  # the group exists before the fact lands ('$')
        event = _complete(row).payload
        await AsyncBusPublisher(client).execute(BusPublish(streams.CONTENT_DERIVED, to_wire(event)))

        async def _until_applied():
            while not defer.calls:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_until_applied(), timeout=5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert row.status == ProcessCommandStatus.COMPLETED
        pending = await client.xpending(streams.CONTENT_DERIVED, CONSUMER_GROUP)
        assert pending["pending"] == 0

    async def test_a_contradictory_fact_is_acked_past(self, db_session):
        # empty=True beside a digest fails decode on the consumer class; the
        # loop must ack past it rather than re-read it forever.
        client = fakeredis.FakeAsyncRedis()
        stop = asyncio.Event()

        @asynccontextmanager
        async def _ctx():
            yield db_session

        task = asyncio.create_task(
            run_derived_consumer(client, lambda: _ctx(), stop=stop, block_ms=10)
        )
        await asyncio.sleep(0.1)
        frame = to_wire(_complete("01CONTRADICTIONXXXXXXXXXXX").payload)
        frame["payload"] = frame["payload"].replace('"empty":false', '"empty":true')
        await client.xadd(streams.CONTENT_DERIVED, frame)

        async def _until_acked():
            while True:
                pending = await client.xpending(streams.CONTENT_DERIVED, CONSUMER_GROUP)
                info = await client.xinfo_groups(streams.CONTENT_DERIVED)
                if pending["pending"] == 0 and info[0]["last-delivered-id"] != b"0-0":
                    return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_until_acked(), timeout=5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)


class TestSettlingLocksTheRow:
    """CR 2, the consumer's half: the row is read ``FOR UPDATE`` before settling."""

    async def test_the_row_read_locks(self, db_session):
        row = await _issued(db_session)
        statements: list[str] = []

        def _capture(conn, cursor, statement, *args):
            statements.append(statement)

        engine = db_session.bind.engine.sync_engine
        fact = _complete(row)
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            await process_derived_message(db_session, fact, defer=_DeferSpy())
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        reads = [sql for sql in statements if sql.lstrip().startswith("SELECT")]
        assert reads and "FOR UPDATE" in reads[0], reads
