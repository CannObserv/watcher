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
from sqlalchemy import select

import src.workers.fetch_commands as fc_mod
import src.workers.process_commands as pc_mod
from src.core.fetch_commands import FETCH_MAX_REISSUES_ENV, create_fetch_command
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.models.process_command import (
    LocalOutcome,
    ProcessCommand,
    ProcessCommandStatus,
    ShadowVerdict,
)
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.process_commands import (
    EXTRACT_MODE_ENV,
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
        """Another command answered recently: the processor is consuming."""
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
        assert "processor not consuming" in caplog.text

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
