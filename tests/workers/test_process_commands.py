"""Tests for the content.process worker tasks (#325): sweep, apply, reaper.

Shadow mode's leg is a side lineage: these tasks write ``process_commands``
rows, the comparator's verdict, a mismatch audit — and nothing on the fetch row
or the item, which local extraction still owns.
"""

import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.extract import CANONICAL_TEXT_MEDIA_TYPE
from co_core.pure.models.changes import ProcessingCompleteEmit
from sqlalchemy import event, select

import src.workers.fetch_commands as fc_mod
import src.workers.pipeline as pipeline_mod
import src.workers.process_commands as pc_mod
from src.core.extract_mode import EXTRACT_MODE_ENV
from src.core.fetch_commands import (
    FETCH_MAX_REISSUES_ENV,
    create_fetch_command,
    get_open_command,
)
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.change_revision import ChangeRevision
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.process_command import (
    LocalOutcome,
    ProcessCommand,
    ProcessCommandStatus,
    ShadowVerdict,
)
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.process_commands import (
    PROCESS_COMMAND_HARD_LIMIT_ENV,
    PROCESS_COMMAND_TIMEOUT_ENV,
    LocalExtraction,
    create_process_command,
)
from src.core.registry import ServiceRegistry
from src.workers.derived_facts import process_derived_message
from src.workers.fetch_commands import apply_fetch_blob
from src.workers.pipeline import _extract_and_fingerprint
from src.workers.process_commands import (
    apply_process_fact,
    publish_pending_process_commands,
    reap_process_commands,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

RAW_DIGEST = "61" * 32
LOCAL_DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "ef" * 32
SPEC_A = {"extraction": {"selector": "div.content", "algorithm": "css"}, "schema_version": 1}
SPEC_B = {"extraction": {"selector": "main", "algorithm": "css"}, "schema_version": 1}


def _wire(db_session, monkeypatch):
    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock(side_effect=lambda: _ctx())
    monkeypatch.setattr(pc_mod, "get_session_factory", lambda: factory)


async def _command(
    db_session,
    *,
    specs=(SPEC_A, SPEC_B),
    local=LocalExtraction(outcome=LocalOutcome.UNCHANGED, fingerprint=LOCAL_DIGEST),
    issued_at=None,
    status=ProcessCommandStatus.IN_FLIGHT,
    **fields,
) -> ProcessCommand:
    issued_at = issued_at or datetime.now(UTC)
    wi = await make_watched_item(
        db_session, primary_url="https://lcb.wa.gov/boardmeetings", source_specs=list(specs)
    )
    fetch = await create_fetch_command(db_session, wi, now=issued_at)
    fetch.status = FetchCommandStatus.SUCCEEDED
    fetch.content_fingerprint = RAW_DIGEST
    fetch.blob_uri = f"gs://co-gcs-blobs/blobs/{RAW_DIGEST}.bin"
    await db_session.flush()
    row = await create_process_command(db_session, fetch, wi, now=issued_at, local=local)
    row.status = status
    if status != ProcessCommandStatus.PENDING_PUBLISH:
        row.published_at = issued_at
    for key, value in fields.items():
        setattr(row, key, value)
    await db_session.flush()
    return row


def _completed(**over):
    return {
        "status": ProcessCommandStatus.COMPLETED,
        "fact_at": datetime.now(UTC),
        "empty": False,
        "output_digest": LOCAL_DIGEST,
        "output_uri": f"gs://co-gcs-processor/blobs/{'ab' * 32}.bin",
        "output_size_bytes": 10,
        "processor_version": "0.19.7+1",
        **over,
    }


def _empty(**over):
    return _completed(empty=True, output_digest=None, output_uri=None, output_size_bytes=0, **over)


async def _rows(db_session, intent_id) -> list[ProcessCommand]:
    stmt = (
        select(ProcessCommand)
        .where(ProcessCommand.intent_id == intent_id)
        .order_by(ProcessCommand.issued_at, ProcessCommand.spec_index)
    )
    return list((await db_session.execute(stmt)).scalars().all())


async def _mismatch_audits(db_session) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.event_type == EventType.CHECK_SHADOW_MISMATCH)
    return list((await db_session.execute(stmt)).scalars().all())


class TestPublishPendingProcessCommands:
    async def test_republishes_under_the_same_id(self, db_session):
        row = await _command(db_session, status=ProcessCommandStatus.PENDING_PUBLISH)
        client = fakeredis.FakeAsyncRedis()

        result = await publish_pending_process_commands(session=db_session, bus_client=client)

        assert result == {"published": 1}
        assert row.status == ProcessCommandStatus.IN_FLIGHT
        assert await client.xlen("content.process") == 1

    async def test_idle_sweep_without_a_bus_is_quiet(self, db_session, monkeypatch, caplog):
        monkeypatch.setattr(pc_mod, "get_shared_bus_client", lambda: None)
        with caplog.at_level(logging.ERROR):
            result = await publish_pending_process_commands(session=db_session)
        assert result == {"published": 0}
        assert not caplog.records


class TestApplyProcessFact:
    async def test_equal_digest_is_judged_a_match(self, db_session, monkeypatch):
        row = await _command(db_session, **_completed())
        _wire(db_session, monkeypatch)

        result = await apply_process_fact(row.command_id)

        assert result == {"verdict": "match", "detail": None}
        assert row.shadow_verdict == ShadowVerdict.MATCH
        assert row.applied_at is not None
        assert await _mismatch_audits(db_session) == []

    async def test_mismatch_is_logged_and_audited(self, db_session, monkeypatch, caplog):
        row = await _command(db_session, **_completed(output_digest=OTHER_DIGEST))
        _wire(db_session, monkeypatch)

        with caplog.at_level(logging.WARNING):
            result = await apply_process_fact(row.command_id)

        assert result["verdict"] == "mismatch"
        assert row.shadow_verdict == ShadowVerdict.MISMATCH
        assert "shadow extraction mismatch" in caplog.text
        (event,) = await _mismatch_audits(db_session)
        assert event.payload["watched_item_id"] == str(row.watched_item_id)
        assert event.payload["command_id"] == row.command_id
        assert event.payload["local_fingerprint"] == LOCAL_DIGEST
        assert event.payload["output_digest"] == OTHER_DIGEST

    async def test_the_shadow_leg_never_touches_the_item_or_the_fetch_row(
        self, db_session, monkeypatch
    ):
        row = await _command(db_session, **_completed(output_digest=OTHER_DIGEST))
        fetch = await db_session.get(FetchCommand, row.fetch_command_id)
        item = await db_session.get(WatchedItem, row.watched_item_id)
        before = (fetch.status, fetch.applied_at, item.health_status, item.last_checked_at)
        _wire(db_session, monkeypatch)

        await apply_process_fact(row.command_id)

        assert row.shadow_verdict == ShadowVerdict.MISMATCH
        after = (fetch.status, fetch.applied_at, item.health_status, item.last_checked_at)
        assert after == before

    async def test_empty_with_a_spec_left_chains_the_next_spec(self, db_session, monkeypatch):
        row = await _command(db_session, **_empty())
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        assert result["spec_index"] == 1
        first, nxt = await _rows(db_session, row.intent_id)
        assert first.command_id == row.command_id
        assert first.applied_at is not None
        assert first.shadow_verdict is None  # the lineage is not over
        assert nxt.command_id == result["chained"]
        assert nxt.spec_index == 1
        assert nxt.source_spec == SPEC_B
        assert nxt.status == ProcessCommandStatus.IN_FLIGHT
        assert await client.xlen("content.process") == 1

    async def test_empty_on_the_last_spec_ends_the_lineage(self, db_session, monkeypatch):
        local_failed = LocalExtraction(outcome=LocalOutcome.EXTRACTION_FAILED)
        row = await _command(db_session, local=local_failed, spec_index=1, **_empty())
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        assert result == {"verdict": "match", "detail": None}
        assert await client.xlen("content.process") == 0

    async def test_specs_shrunk_mid_chain_end_the_lineage(self, db_session, monkeypatch):
        row = await _command(db_session, specs=(SPEC_A,), **_empty())
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        # Local derived text and the processor found nothing: a disagreement.
        assert result["verdict"] == "mismatch"
        assert await client.xlen("content.process") == 0

    async def test_input_failure_is_uncompared(self, db_session, monkeypatch):
        row = await _command(
            db_session,
            status=ProcessCommandStatus.FAILED,
            fact_at=datetime.now(UTC),
            failure_reason="input_unreadable",
        )
        _wire(db_session, monkeypatch)

        result = await apply_process_fact(row.command_id)

        assert result == {"verdict": "uncompared", "detail": "input_unreadable"}
        assert await _mismatch_audits(db_session) == []

    async def test_runs_at_most_once(self, db_session, monkeypatch):
        row = await _command(db_session, **_completed())
        _wire(db_session, monkeypatch)

        await apply_process_fact(row.command_id)
        again = await apply_process_fact(row.command_id)

        assert again == {"skipped": True, "reason": "already_applied"}

    async def test_unsettled_row_is_skipped(self, db_session, monkeypatch):
        row = await _command(db_session)
        _wire(db_session, monkeypatch)

        assert await apply_process_fact(row.command_id) == {
            "skipped": True,
            "reason": "status_in_flight",
        }
        assert row.applied_at is None


class TestReapProcessCommands:
    """#325's downtime rule: delayed, never failed — and never duplicated."""

    def _stale(self):
        return datetime.now(UTC) - timedelta(hours=1)

    async def _fresh_fact_elsewhere(self, db_session):
        """A command published *after* the stale one was answered: the processor
        read past it, so the stale one is stuck rather than queued."""
        return await _command(db_session, **_completed(fact_at=datetime.now(UTC)))

    async def test_stuck_command_is_reissued_while_the_processor_consumes(self, db_session):
        await self._fresh_fact_elsewhere(db_session)
        row = await _command(db_session, issued_at=self._stale())
        client = fakeredis.FakeAsyncRedis()

        result = await reap_process_commands(session=db_session, bus_client=client)

        assert result["reissued"] == 1
        assert row.status == ProcessCommandStatus.EXPIRED
        old, new = await _rows(db_session, row.intent_id)
        assert new.reissue_count == 1
        assert new.spec_index == row.spec_index
        assert new.status == ProcessCommandStatus.IN_FLIGHT
        assert await client.xlen("content.process") == 1

    async def test_a_backlog_draining_in_order_is_not_reissued(self, db_session, caplog):
        # CR 1. The processor is back and has answered the first of two commands
        # that queued during its outage; the second is stale but still queued
        # behind it. A fact for an *earlier* command says nothing about this one.
        two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
        await _command(db_session, issued_at=two_hours_ago, **_completed(fact_at=datetime.now(UTC)))
        queued = await _command(db_session, issued_at=two_hours_ago + timedelta(minutes=1))
        client = fakeredis.FakeAsyncRedis()

        with caplog.at_level(logging.WARNING):
            result = await reap_process_commands(session=db_session, bus_client=client)

        assert result["reissued"] == 0
        assert result["held"] == 1
        assert queued.status == ProcessCommandStatus.IN_FLIGHT
        assert await client.xlen("content.process") == 0

    async def test_nothing_is_reissued_while_the_processor_is_down(self, db_session, caplog):
        # No fact for any command inside the window: the command waits in the
        # processor's group, and a re-issue would only add a duplicate.
        row = await _command(db_session, issued_at=self._stale())
        client = fakeredis.FakeAsyncRedis()

        with caplog.at_level(logging.WARNING):
            result = await reap_process_commands(session=db_session, bus_client=client)

        assert result["held"] == 1
        assert result["reissued"] == 0
        assert row.status == ProcessCommandStatus.IN_FLIGHT
        assert await client.xlen("content.process") == 0
        # Down or still draining its backlog in order, the processor has not
        # reached it — the one line says that much and no more (CR 10).
        assert "processor has not reached" in caplog.text
        (held_line,) = [r for r in caplog.records if "has not reached" in r.getMessage()]
        # AGENTS.md: ISO 8601 with a Z, never +00:00 (CR 6).
        assert held_line.oldest_issued_at.endswith("Z")

    async def test_cap_ends_the_lineage_uncompared(self, db_session, monkeypatch):
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "2")
        await self._fresh_fact_elsewhere(db_session)
        row = await _command(db_session, issued_at=self._stale(), reissue_count=2)
        client = fakeredis.FakeAsyncRedis()

        result = await reap_process_commands(session=db_session, bus_client=client)

        assert result["capped"] == 1
        assert row.status == ProcessCommandStatus.EXPIRED
        assert row.shadow_verdict == ShadowVerdict.UNCOMPARED
        assert row.shadow_detail.startswith("processing_timeout")
        assert await client.xlen("content.process") == 0

    async def test_hard_limit_ends_a_lineage_even_while_the_processor_is_down(
        self, db_session, monkeypatch
    ):
        # The processor#17 residual: a refused failure fact leaves one command
        # with no reply, and a quiet period offers no other fact to go on.
        monkeypatch.setenv(PROCESS_COMMAND_HARD_LIMIT_ENV, "7200")
        row = await _command(db_session, issued_at=datetime.now(UTC) - timedelta(hours=3))

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        assert result["hard_limited"] == 1
        assert row.status == ProcessCommandStatus.EXPIRED
        assert row.shadow_verdict == ShadowVerdict.UNCOMPARED
        assert row.applied_at is not None

    async def test_fresh_commands_are_left_alone(self, db_session):
        await self._fresh_fact_elsewhere(db_session)
        row = await _command(db_session)

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        assert result["reissued"] == result["held"] == 0
        assert row.status == ProcessCommandStatus.IN_FLIGHT

    async def test_a_lost_apply_is_redeferred_without_faking_liveness(
        self, db_session, monkeypatch
    ):
        monkeypatch.setenv(PROCESS_COMMAND_TIMEOUT_ENV, "60")
        old_fact = datetime.now(UTC) - timedelta(hours=1)
        row = await _command(db_session, **_completed(fact_at=old_fact))
        row.updated_at = old_fact
        await db_session.flush()
        defer = AsyncMock()
        monkeypatch.setattr(pc_mod, "_defer_reapply", defer)

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        assert result["reapplied"] == 1
        defer.assert_awaited_once_with(row.command_id)
        # fact_at is the consuming signal; the reaper must not refresh it.
        assert row.fact_at == old_fact


class TestShadowEndToEnd:
    """Blob applied → command on the bus → fake processor → fact → verdict (#325).

    The fake processor runs the same co-core extraction the real one does, over
    exactly the one spec and essence the command names — so a match here means
    the command carried everything a processor needs to reproduce local's answer.
    """

    HTML = b"<html><body><div class='a'></div><main>Board meets Tuesday</main></body></html>"
    # spec[0] binds nothing, so the chain must reach spec[1] — as local's loop did.
    SPECS = [
        {"extraction": {"algorithm": "css", "selector": "div.a"}, "schema_version": 1},
        {"extraction": {"algorithm": "css", "selector": "main"}, "schema_version": 1},
    ]

    async def _applied_blob(self, db_session, monkeypatch, tmp_path, client):
        monkeypatch.setenv(EXTRACT_MODE_ENV, "shadow")
        blob = tmp_path / "blob.bin"
        blob.write_bytes(self.HTML)
        wi = await make_watched_item(
            db_session, primary_url="https://lcb.wa.gov/boardmeetings", source_specs=self.SPECS
        )
        fetch = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        fetch.status = FetchCommandStatus.IN_FLIGHT
        fetch.published_at = fetch.fact_at = datetime.now(UTC)
        fetch.blob_uri = f"file://{blob}"
        fetch.content_fingerprint = hashlib.sha256(self.HTML).hexdigest()
        await db_session.flush()
        _wire(db_session, monkeypatch)
        monkeypatch.setattr(fc_mod, "get_session_factory", pc_mod.get_session_factory)
        result = await apply_fetch_blob(
            fetch.command_id, registry=ServiceRegistry(), bus_client=client
        )
        assert result["applied"] is True
        return wi, fetch

    async def _commands(self, client, *, after="-"):
        decoded = []
        for message_id, fields in await client.xrange(streams.CONTENT_PROCESS, min=after):
            frame = {k.decode(): v.decode() for k, v in fields.items()}
            decoded.append((message_id, from_wire(frame, topic=streams.CONTENT_PROCESS).payload))
        return decoded

    def _answer(self, command):
        """What a processor running co-core 0.19.7 publishes for ``command``."""
        extractor = ServiceRegistry().get_extractor(command.media_type)
        outcome = _extract_and_fingerprint(self.HTML, [command.source_spec], extractor=extractor)
        empty = outcome.content_size_bytes == 0
        event = ProcessingCompleteEmit(
            occurred_at=datetime.now(UTC),
            command_id=command.command_id,
            info_source_id=command.info_source_id,
            empty=empty,
            output_digest=None if empty else outcome.content_fingerprint,
            output_uri=None if empty else "gs://co-gcs-processor/blobs/x.bin",
            output_size_bytes=outcome.content_size_bytes,
            output_media_type=CANONICAL_TEXT_MEDIA_TYPE,
            spec_schema_version=outcome.schema_version,
            processor_version=outcome.processor_version,
            spec_fingerprint=outcome.spec_fingerprint,
        )
        return from_wire(to_wire(event), topic=streams.CONTENT_DERIVED, message_id="1-1")

    async def _deliver(self, db_session, client, message):
        async def _apply_now(command_id):
            await apply_process_fact(command_id, bus_client=client)

        return await process_derived_message(db_session, message, defer=_apply_now)

    async def test_the_chain_reproduces_local_and_matches(self, db_session, monkeypatch, tmp_path):
        client = fakeredis.FakeAsyncRedis()
        wi, fetch = await self._applied_blob(db_session, monkeypatch, tmp_path, client)

        ((_, first),) = await self._commands(client)
        assert first.source_spec == self.SPECS[0]
        await self._deliver(db_session, client, self._answer(first))  # empty → chains

        commands = await self._commands(client)
        assert len(commands) == 2
        second = commands[1][1]
        assert second.source_spec == self.SPECS[1]
        await self._deliver(db_session, client, self._answer(second))

        rows = await _rows(
            db_session, (await db_session.get(ProcessCommand, first.command_id)).intent_id
        )
        assert [r.shadow_verdict for r in rows] == [None, ShadowVerdict.MATCH]
        assert rows[1].local_outcome == LocalOutcome.BASELINE
        assert await _mismatch_audits(db_session) == []

    async def test_an_outage_delays_the_verdict_and_duplicates_nothing(
        self, db_session, monkeypatch, tmp_path
    ):
        # The processor is down: nothing answers, the command goes stale.
        client = fakeredis.FakeAsyncRedis()
        wi, fetch = await self._applied_blob(db_session, monkeypatch, tmp_path, client)
        ((_, command),) = await self._commands(client)
        row = await db_session.get(ProcessCommand, command.command_id)
        row.published_at = datetime.now(UTC) - timedelta(hours=2)
        await db_session.flush()

        held = await reap_process_commands(session=db_session, bus_client=client)

        assert held["held"] == 1 and held["reissued"] == 0
        assert len(await self._commands(client)) == 1  # no duplicate on the stream
        fetch_after = await db_session.get(FetchCommand, fetch.command_id)
        assert fetch_after.status == FetchCommandStatus.SUCCEEDED  # local decided
        assert wi.health_status == WatchHealthStatus.OK  # and the item never knew

        # The processor comes back and works through what waited for it.
        await self._deliver(db_session, client, self._answer(command))
        nxt = (await self._commands(client))[1][1]
        await self._deliver(db_session, client, self._answer(nxt))

        rows = await _rows(db_session, row.intent_id)
        assert rows[-1].shadow_verdict == ShadowVerdict.MATCH
        assert len(await self._commands(client)) == 2  # spec[0], spec[1] — nothing else


class TestReaperLocksWhatItReaps:
    """CR 2: the reaper and the consumer must not both act on one in-flight row.

    Unlocked, a reaper that loaded the row before the consumer settled it would
    commit EXPIRED over COMPLETED — the answer lost, a duplicate re-issued.
    ``FOR UPDATE`` on both sides lets Postgres re-check ``status = in_flight``
    after the wait, so whichever runs second sees the first one's outcome.
    """

    def _stale(self):
        return datetime.now(UTC) - timedelta(hours=1)

    async def test_each_stale_row_is_reread_locked(self, db_session):
        await _command(db_session, issued_at=self._stale())
        statements: list[str] = []

        def _capture(conn, cursor, statement, *args):
            statements.append(statement)

        engine = db_session.bind.engine.sync_engine
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            await reap_process_commands(session=db_session, bus_client=fakeredis.FakeAsyncRedis())
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        locked = [
            sql for sql in statements if "FROM process_commands" in sql and "FOR UPDATE" in sql
        ]
        assert locked, statements

    async def test_a_row_settled_meanwhile_is_left_alone(self, db_session, monkeypatch):
        await _command(db_session, issued_at=datetime.now(UTC), **_completed())  # read past it
        row = await _command(db_session, issued_at=self._stale())
        real_refresh = db_session.refresh

        async def _settled_by_the_consumer(obj, **kwargs):
            await real_refresh(obj, **kwargs)
            if obj is row:
                obj.status = ProcessCommandStatus.COMPLETED  # what the locked read would see

        monkeypatch.setattr(db_session, "refresh", _settled_by_the_consumer)
        client = fakeredis.FakeAsyncRedis()

        result = await reap_process_commands(session=db_session, bus_client=client)

        assert result["reissued"] == 0
        assert row.status == ProcessCommandStatus.COMPLETED
        assert await client.xlen("content.process") == 0


async def _decisive(
    db_session,
    *,
    specs=(SPEC_A, SPEC_B),
    issued_at=None,
    fetch_over=None,
    item_over=None,
    status=ProcessCommandStatus.IN_FLIGHT,
    **fields,
) -> ProcessCommand:
    """A processor-mode occasion: the fetch row PROCESSING, its command in flight."""
    issued_at = issued_at or datetime.now(UTC)
    wi = await make_watched_item(
        db_session,
        primary_url="https://lcb.wa.gov/boardmeetings",
        source_specs=list(specs),
        **(item_over or {}),
    )
    fetch = await create_fetch_command(db_session, wi, now=issued_at)
    fetch.status = FetchCommandStatus.PROCESSING
    fetch.published_at = fetch.fact_at = issued_at
    fetch.content_fingerprint = RAW_DIGEST
    fetch.blob_uri = f"gs://co-gcs-blobs/blobs/{RAW_DIGEST}.bin"
    fetch.media_type = "text/html"
    for key, value in (fetch_over or {}).items():
        setattr(fetch, key, value)
    await db_session.flush()
    row = await create_process_command(
        db_session, fetch, wi, now=issued_at, local=None, reissue_count=fetch.reissue_count
    )
    row.status = status
    if status != ProcessCommandStatus.PENDING_PUBLISH:
        row.published_at = issued_at
    for key, value in fields.items():
        setattr(row, key, value)
    await db_session.flush()
    return row


def _quiet(monkeypatch) -> AsyncMock:
    """Stub every notification the decisive path can send; returns the change spy."""
    monkeypatch.setattr(fc_mod, "dispatch_event_notifications", AsyncMock(return_value=0))
    change = AsyncMock(return_value=0)
    monkeypatch.setattr(pipeline_mod, "dispatch_event_notifications", change)
    return change


async def _audits(db_session, event_type) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.event_type == event_type)
    return list((await db_session.execute(stmt)).scalars().all())


async def _revisions(db_session, watched_item_id) -> list[ChangeRevision]:
    stmt = (
        select(ChangeRevision)
        .where(ChangeRevision.watched_item_id == watched_item_id)
        .order_by(ChangeRevision.captured_at)
    )
    return list((await db_session.execute(stmt)).scalars().all())


class TestDecisiveApply:
    """Processor mode (#326): the derived fact decides and closes the check.

    A lineage is decisive when its fetch row is ``PROCESSING`` — fixed when the
    blob applied, so a fact still decides after the mode is turned back, and a
    shadow lineage is still only judged after it is turned on.
    """

    async def _fetch(self, db_session, row) -> FetchCommand:
        return await db_session.get(FetchCommand, row.fetch_command_id)

    async def _item(self, db_session, row) -> WatchedItem:
        return await db_session.get(WatchedItem, row.watched_item_id)

    async def test_a_first_answer_baselines_and_closes_the_check(self, db_session, monkeypatch):
        row = await _decisive(
            db_session, fetch_over={"etag": '"v1"'}, **_completed(spec_fingerprint="spec1:x")
        )
        _wire(db_session, monkeypatch)
        change = _quiet(monkeypatch)

        result = await apply_process_fact(row.command_id)

        fetch, item = await self._fetch(db_session, row), await self._item(db_session, row)
        assert result["applied"] is True and result["baseline_established"] is True
        assert fetch.status == FetchCommandStatus.SUCCEEDED
        assert fetch.applied_at is not None
        assert item.health_status == WatchHealthStatus.OK
        assert item.last_checked_at is not None
        assert item.processor_version == "0.19.7+1"
        # The pair is stored with the outcome it vouches for.
        assert item.etag == '"v1"'
        (baseline,) = await _revisions(db_session, item.id)
        assert baseline.content_fingerprint == LOCAL_DIGEST
        assert baseline.spec_fingerprint == "spec1:x"
        assert baseline.processor_version == "0.19.7+1"
        change.assert_not_awaited()
        (event,) = await _audits(db_session, EventType.CHECK_SNAPSHOT_CREATED)
        assert event.payload["source"] == "processor"
        assert row.shadow_verdict is None  # decided, not judged

    async def test_a_new_digest_is_a_change(self, db_session, monkeypatch):
        row = await _decisive(db_session, **_completed(output_digest=OTHER_DIGEST))
        item = await self._item(db_session, row)
        db_session.add(
            ChangeRevision(
                watched_item_id=item.id,
                content_fingerprint=LOCAL_DIGEST,
                captured_at=datetime.now(UTC) - timedelta(days=1),
                content_size_bytes=10,
                schema_version=1,
                processor_version="0.19.7+1",
            )
        )
        item.processor_version = "0.19.7+1"
        await db_session.flush()
        _wire(db_session, monkeypatch)
        change = _quiet(monkeypatch)

        result = await apply_process_fact(row.command_id)

        assert result["changed"] is True
        change.assert_awaited_once()
        sync = (
            await db_session.execute(
                select(PendingArchiverSync).where(PendingArchiverSync.watched_item_id == item.id)
            )
        ).scalar_one()
        # Raw-blob provenance still comes from the fetch fact.
        assert sync.blob_uri == (await self._fetch(db_session, row)).blob_uri
        assert sync.blob_fingerprint == RAW_DIGEST
        assert sync.source_media_type == "text/html"

    async def test_a_processor_upgrade_alone_re_baselines(self, db_session, monkeypatch):
        row = await _decisive(
            db_session, **_completed(output_digest=OTHER_DIGEST, processor_version="0.20.0+1")
        )
        item = await self._item(db_session, row)
        db_session.add(
            ChangeRevision(
                watched_item_id=item.id,
                content_fingerprint=LOCAL_DIGEST,
                captured_at=datetime.now(UTC) - timedelta(days=1),
                content_size_bytes=10,
                schema_version=1,
                processor_version="0.19.7+1",
            )
        )
        item.processor_version = "0.19.7+1"
        await db_session.flush()
        _wire(db_session, monkeypatch)
        change = _quiet(monkeypatch)

        result = await apply_process_fact(row.command_id)

        assert result["rebaselined"] is True
        change.assert_not_awaited()
        assert len(await _audits(db_session, EventType.CHECK_REBASELINED)) == 1
        (event,) = await _audits(db_session, EventType.CHECK_SNAPSHOT_CREATED)
        assert event.payload["rebaselined"] is True

    async def test_empty_with_a_spec_left_chains_and_stays_open(self, db_session, monkeypatch):
        row = await _decisive(db_session, **_empty())
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        assert result["spec_index"] == 1
        _first, nxt = await _rows(db_session, row.intent_id)
        assert nxt.local_outcome is None
        assert (await self._fetch(db_session, row)).status == FetchCommandStatus.PROCESSING

    async def test_empty_on_the_last_spec_is_an_extraction_failure(self, db_session, monkeypatch):
        row = await _decisive(
            db_session,
            specs=(SPEC_A,),
            item_over={"etag": '"old"', "validator_source_key": "sha256:k"},
            **_empty(),
        )
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)

        result = await apply_process_fact(row.command_id)

        fetch, item = await self._fetch(db_session, row), await self._item(db_session, row)
        assert result == {"error": "extraction_failed"}
        assert fetch.status == FetchCommandStatus.FAILED
        assert fetch.failure_reason == "processing_failed"
        assert "empty" in fetch.failure_detail
        assert item.health_status == WatchHealthStatus.ERROR
        assert item.etag is None  # #269: the next fetch is in full
        assert await _revisions(db_session, item.id) == []
        assert len(await _audits(db_session, EventType.CHECK_EXTRACTION_FAILED)) == 1

    @pytest.mark.parametrize(
        "reason", ["extraction_error", "unsupported_media_type", "invalid_input"]
    )
    async def test_a_terminal_failure_is_an_extraction_failure(
        self, db_session, monkeypatch, reason
    ):
        # invalid_input included: the processor refused the reference itself,
        # and a re-fetch would mint the same kind of reference again.
        row = await _decisive(
            db_session,
            status=ProcessCommandStatus.FAILED,
            fact_at=datetime.now(UTC),
            failure_reason=reason,
            failure_detail="dead-lettered: parser blew up",
        )
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        fetch = await self._fetch(db_session, row)
        assert result == {"error": "extraction_failed"}
        assert fetch.status == FetchCommandStatus.FAILED
        assert fetch.failure_reason == "processing_failed"
        (event,) = await _audits(db_session, EventType.CHECK_EXTRACTION_FAILED)
        assert event.payload["detail"] == f"{reason}: dead-lettered: parser blew up"
        assert await client.xlen("content.fetch") == 0

    async def test_input_unreadable_re_fetches_under_the_same_intent(self, db_session, monkeypatch):
        row = await _decisive(
            db_session,
            status=ProcessCommandStatus.FAILED,
            fact_at=datetime.now(UTC),
            failure_reason="input_unreadable",
            failure_detail="blob gone",
        )
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        fetch = await self._fetch(db_session, row)
        assert fetch.status == FetchCommandStatus.EXPIRED
        reissued = await db_session.get(FetchCommand, result["reissued"])
        assert reissued.intent_id == fetch.intent_id
        assert reissued.reissue_count == 1
        assert await client.xlen("content.fetch") == 1
        item = await self._item(db_session, row)
        assert item.health_status != WatchHealthStatus.ERROR

    async def test_input_unreadable_at_the_cap_fails_the_check(self, db_session, monkeypatch):
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "2")
        # The process leg re-issued past the fetch leg: the lineage counts both.
        row = await _decisive(
            db_session,
            fetch_over={"reissue_count": 1},
            status=ProcessCommandStatus.FAILED,
            fact_at=datetime.now(UTC),
            failure_reason="input_unreadable",
            reissue_count=2,
        )
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        fetch = await self._fetch(db_session, row)
        assert result["error"] == "blob_unreadable"
        assert fetch.status == FetchCommandStatus.FAILED
        assert fetch.failure_reason == "blob_unreadable"
        assert (await self._item(db_session, row)).health_status == WatchHealthStatus.ERROR
        assert await client.xlen("content.fetch") == 0

    async def test_a_superseded_occasion_writes_nothing(self, db_session, monkeypatch):
        row = await _decisive(db_session, **_completed())
        item = await self._item(db_session, row)
        fetch = await self._fetch(db_session, row)
        newer = await create_fetch_command(
            db_session, item, now=fetch.issued_at + timedelta(minutes=5)
        )
        newer.status = FetchCommandStatus.SUCCEEDED
        newer.applied_at = newer.issued_at
        await db_session.flush()
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)

        result = await apply_process_fact(row.command_id)

        assert result == {"skipped": True, "reason": "superseded"}
        assert fetch.status == FetchCommandStatus.SUPERSEDED
        assert await _revisions(db_session, item.id) == []

    async def test_a_shadow_lineage_is_still_only_judged(self, db_session, monkeypatch):
        # Issued under shadow, its fetch row closed by local: turning processor
        # mode on must not let the late answer decide a check already decided.
        monkeypatch.setenv(EXTRACT_MODE_ENV, "processor")
        row = await _command(db_session, **_completed())
        _wire(db_session, monkeypatch)

        result = await apply_process_fact(row.command_id)

        assert result == {"verdict": "match", "detail": None}
        assert (await self._fetch(db_session, row)).status == FetchCommandStatus.SUCCEEDED


class TestDecisiveReaper:
    """The reaper's give-ups close a decisive check (#326's downtime rule).

    Held, an item waits — *processing delayed*, never ERROR. Past the hard
    limit or the re-issue cap, the fetch row fails ``processing_timeout`` and
    the item goes ERROR, so the one-open-command gate lifts.
    """

    def _stale(self):
        return datetime.now(UTC) - timedelta(hours=1)

    async def _fetch(self, db_session, row) -> FetchCommand:
        return await db_session.get(FetchCommand, row.fetch_command_id)

    async def test_held_items_wait_without_error(self, db_session, monkeypatch, caplog):
        row = await _decisive(db_session, issued_at=self._stale())
        _quiet(monkeypatch)

        with caplog.at_level(logging.WARNING):
            result = await reap_process_commands(
                session=db_session, bus_client=fakeredis.FakeAsyncRedis()
            )

        assert result["held"] == 1
        item = await db_session.get(WatchedItem, row.watched_item_id)
        assert item.health_status != WatchHealthStatus.ERROR
        assert (await self._fetch(db_session, row)).status == FetchCommandStatus.PROCESSING
        (line,) = [r for r in caplog.records if "processing delayed" in r.getMessage()]
        assert line.watched_item_ids == [str(row.watched_item_id)]

    async def test_the_hard_limit_fails_the_check(self, db_session, monkeypatch):
        monkeypatch.setenv(PROCESS_COMMAND_HARD_LIMIT_ENV, "7200")
        row = await _decisive(db_session, issued_at=datetime.now(UTC) - timedelta(hours=3))
        _quiet(monkeypatch)

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        fetch = await self._fetch(db_session, row)
        assert result["hard_limited"] == 1
        assert row.status == ProcessCommandStatus.EXPIRED
        assert fetch.status == FetchCommandStatus.FAILED
        assert fetch.failure_reason == "processing_timeout"
        assert fetch.applied_at is not None
        item = await db_session.get(WatchedItem, row.watched_item_id)
        assert item.health_status == WatchHealthStatus.ERROR
        (event,) = await _audits(db_session, EventType.CHECK_EXTRACTION_FAILED)
        assert event.payload["reason"] == "processing_timeout"

    async def test_the_re_issue_cap_fails_the_check(self, db_session, monkeypatch):
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "2")
        await _command(db_session, issued_at=datetime.now(UTC), **_completed())  # read past
        row = await _decisive(db_session, issued_at=self._stale(), reissue_count=2)
        _quiet(monkeypatch)

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        assert result["capped"] == 1
        assert (await self._fetch(db_session, row)).failure_reason == "processing_timeout"


class TestUnpublishedPastTheHardLimit:
    """CR 6: a command the bus never accepted still ends at the hard limit.

    The sweep retries a refused publish every minute (a broker ACL
    ``NoPermissionError`` is transient by policy), so without this a decisive
    check would sit ``PROCESSING`` forever: never re-checked, never ERROR.
    """

    async def test_a_decisive_check_fails(self, db_session, monkeypatch):
        monkeypatch.setenv(PROCESS_COMMAND_HARD_LIMIT_ENV, "7200")
        row = await _decisive(
            db_session,
            issued_at=datetime.now(UTC) - timedelta(hours=3),
            status=ProcessCommandStatus.PENDING_PUBLISH,
        )
        _quiet(monkeypatch)

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        fetch = await db_session.get(FetchCommand, row.fetch_command_id)
        assert result["hard_limited"] == 1
        assert row.status == ProcessCommandStatus.EXPIRED
        assert fetch.status == FetchCommandStatus.FAILED
        assert fetch.failure_reason == "processing_timeout"
        item = await db_session.get(WatchedItem, row.watched_item_id)
        assert item.health_status == WatchHealthStatus.ERROR

    async def test_a_shadow_lineage_ends_uncompared(self, db_session, monkeypatch):
        monkeypatch.setenv(PROCESS_COMMAND_HARD_LIMIT_ENV, "7200")
        row = await _command(
            db_session,
            issued_at=datetime.now(UTC) - timedelta(hours=3),
            status=ProcessCommandStatus.PENDING_PUBLISH,
        )

        await reap_process_commands(session=db_session, bus_client=fakeredis.FakeAsyncRedis())

        assert row.status == ProcessCommandStatus.EXPIRED
        assert row.shadow_verdict == ShadowVerdict.UNCOMPARED

    async def test_a_recent_unpublished_command_is_left_to_the_sweep(self, db_session, monkeypatch):
        row = await _decisive(db_session, status=ProcessCommandStatus.PENDING_PUBLISH)

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        assert result["hard_limited"] == 0
        assert row.status == ProcessCommandStatus.PENDING_PUBLISH


class TestProcessorEndToEnd:
    """Blob applied → command → fake processor → fact → the check decided (#326)."""

    HTML = b"<html><body><main>Board meets Tuesday</main></body></html>"
    HTML_CHANGED = b"<html><body><main>Board meets Thursday</main></body></html>"
    SPECS = [{"extraction": {"algorithm": "css", "selector": "main"}, "schema_version": 1}]

    async def _occasion(self, db_session, monkeypatch, wi, html, client):
        fetch = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        fetch.status = FetchCommandStatus.IN_FLIGHT
        fetch.published_at = fetch.fact_at = datetime.now(UTC)
        fetch.blob_uri = "gs://co-gcs-blobs/blobs/never-read.bin"
        fetch.content_fingerprint = hashlib.sha256(html).hexdigest()
        fetch.media_type = "text/html"
        await db_session.flush()
        result = await apply_fetch_blob(fetch.command_id, bus_client=client)
        assert "processing" in result
        return fetch

    def _answer(self, command, html):
        extractor = ServiceRegistry().get_extractor(command.media_type)
        outcome = _extract_and_fingerprint(html, [command.source_spec], extractor=extractor)
        event = ProcessingCompleteEmit(
            occurred_at=datetime.now(UTC),
            command_id=command.command_id,
            info_source_id=command.info_source_id,
            empty=False,
            output_digest=outcome.content_fingerprint,
            output_uri="gs://co-gcs-processor/blobs/x.bin",
            output_size_bytes=outcome.content_size_bytes,
            output_media_type=CANONICAL_TEXT_MEDIA_TYPE,
            spec_schema_version=outcome.schema_version,
            processor_version=outcome.processor_version,
            spec_fingerprint=outcome.spec_fingerprint,
        )
        return from_wire(to_wire(event), topic=streams.CONTENT_DERIVED, message_id="1-1")

    async def _latest_command(self, client):
        ((_, fields),) = (await client.xrevrange(streams.CONTENT_PROCESS, count=1))[:1]
        frame = {k.decode(): v.decode() for k, v in fields.items()}
        return from_wire(frame, topic=streams.CONTENT_PROCESS).payload

    async def test_baseline_then_a_change(self, db_session, monkeypatch):
        monkeypatch.setenv(EXTRACT_MODE_ENV, "processor")
        _wire(db_session, monkeypatch)
        monkeypatch.setattr(fc_mod, "get_session_factory", pc_mod.get_session_factory)
        change = _quiet(monkeypatch)
        client = fakeredis.FakeAsyncRedis()
        wi = await make_watched_item(
            db_session, primary_url="https://lcb.wa.gov/boardmeetings", source_specs=self.SPECS
        )

        async def _apply_now(command_id):
            await apply_process_fact(command_id, bus_client=client)

        for html in (self.HTML, self.HTML_CHANGED):
            fetch = await self._occasion(db_session, monkeypatch, wi, html, client)
            command = await self._latest_command(client)
            await process_derived_message(db_session, self._answer(command, html), defer=_apply_now)
            assert fetch.status == FetchCommandStatus.SUCCEEDED

        baseline, changed = await _revisions(db_session, wi.id)
        local = _extract_and_fingerprint(self.HTML_CHANGED, self.SPECS)
        # The processor's digest is the stored fingerprint, byte for byte.
        assert changed.content_fingerprint == local.content_fingerprint
        assert wi.health_status == WatchHealthStatus.OK
        change.assert_awaited_once()
        assert await get_open_command(db_session, wi.id) is None
