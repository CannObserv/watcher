"""Tests for the content.process worker tasks (#325, #326): sweep, apply, reaper.

The processor decides every check (#350): a lineage whose fetch row waits
``PROCESSING`` closes it; one whose check already closed decides nothing.
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
from src.core.fetch_commands import (
    FETCH_MAX_REISSUES_ENV,
    create_fetch_command,
    get_open_command,
)
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.change_revision import ChangeRevision
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.process_command import ProcessCommand, ProcessCommandStatus
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.notifications.events import WatchEventType
from src.core.notifications.renotify import ERROR_RENOTIFY_INTERVAL_ENV
from src.core.process_commands import (
    PROCESS_COMMAND_HARD_LIMIT_ENV,
    PROCESS_COMMAND_TIMEOUT_ENV,
    create_process_command,
)
from src.core.utils import format_utc_iso
from src.core.validators import (
    CONDITIONAL_GET_ENV,
    replayable_validators,
    validator_source_key,
)
from src.workers.derived_facts import process_derived_message
from src.workers.fetch_commands import apply_fetch_blob, reissue_fetch_command
from src.workers.process_commands import (
    apply_process_fact,
    publish_pending_process_commands,
    reap_process_commands,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

RAW_DIGEST = "61" * 32
DIGEST = "sha256:" + "ab" * 32
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
    issued_at=None,
    status=ProcessCommandStatus.IN_FLIGHT,
    **fields,
) -> ProcessCommand:
    """An occasion whose check has already closed: the reaper's generic case."""
    issued_at = issued_at or datetime.now(UTC)
    wi = await make_watched_item(
        db_session, primary_url="https://lcb.wa.gov/boardmeetings", source_specs=list(specs)
    )
    fetch = await create_fetch_command(db_session, wi, now=issued_at)
    fetch.status = FetchCommandStatus.SUCCEEDED
    fetch.content_fingerprint = RAW_DIGEST
    fetch.blob_uri = f"gs://co-gcs-blobs/blobs/{RAW_DIGEST}.bin"
    await db_session.flush()
    row = await create_process_command(db_session, fetch, wi, now=issued_at)
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
        "output_digest": DIGEST,
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
    async def test_empty_with_a_spec_left_chains_the_next_spec(self, db_session, monkeypatch):
        row = await _decisive(db_session, **_empty())
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        assert result["spec_index"] == 1
        first, nxt = await _rows(db_session, row.intent_id)
        assert first.command_id == row.command_id
        assert first.applied_at is not None
        assert nxt.command_id == result["chained"]
        assert nxt.spec_index == 1
        assert nxt.source_spec == SPEC_B
        assert nxt.status == ProcessCommandStatus.IN_FLIGHT
        assert await client.xlen("content.process") == 1
        # The check stays open for the chain's answer.
        fetch = await db_session.get(FetchCommand, row.fetch_command_id)
        assert fetch.status == FetchCommandStatus.PROCESSING

    @pytest.mark.parametrize(
        "fact",
        [
            _completed(),
            _empty(),
            _empty(spec_index=1),
            {
                "status": ProcessCommandStatus.FAILED,
                "fact_at": datetime.now(UTC),
                "failure_reason": "input_unreadable",
            },
        ],
        ids=["derived", "empty-with-a-spec-left", "empty-on-the-last-spec", "failed"],
    )
    async def test_an_answer_for_a_closed_check_decides_nothing(
        self, db_session, monkeypatch, fact
    ):
        """The reaper gave up, or a newer occasion closed it: a late answer is
        recorded as applied, touches neither the fetch row nor the item, and
        chains no further spec — nobody is waiting on it."""
        row = await _command(db_session, **fact)
        fetch = await db_session.get(FetchCommand, row.fetch_command_id)
        item = await db_session.get(WatchedItem, row.watched_item_id)
        before = (fetch.status, fetch.applied_at, item.health_status, item.last_checked_at)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_process_fact(row.command_id, bus_client=client)

        assert result == {"skipped": True, "reason": "check_closed"}
        assert row.applied_at is not None
        after = (fetch.status, fetch.applied_at, item.health_status, item.last_checked_at)
        assert after == before
        assert await _revisions(db_session, item.id) == []
        assert await client.xlen("content.fetch") == 0
        assert await client.xlen("content.process") == 0

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

    async def test_cap_ends_the_lineage(self, db_session, monkeypatch):
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "2")
        await self._fresh_fact_elsewhere(db_session)
        row = await _command(db_session, issued_at=self._stale(), reissue_count=2)
        client = fakeredis.FakeAsyncRedis()

        result = await reap_process_commands(session=db_session, bus_client=client)

        assert result["capped"] == 1
        assert row.status == ProcessCommandStatus.EXPIRED
        assert row.applied_at is not None
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
        db_session, fetch, wi, now=issued_at, reissue_count=fetch.reissue_count
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
    """The derived fact decides and closes the check (#326).

    A lineage is decisive when its fetch row is ``PROCESSING`` — the blob apply
    leaves it so, and nothing else does.
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
        assert item.last_observed_at is not None  # verified current (#264)
        assert item.processor_version == "0.19.7+1"
        # The pair is stored with the outcome it vouches for, keyed to the
        # processor version that produced it (#269 — a silent change in this
        # key is the wedge the issue exists to prevent).
        assert item.etag == '"v1"'
        assert item.validator_source_key == validator_source_key(
            effective_url=item.effective_url,
            source_specs=item.source_specs,
            generation="0.19.7+1",
        )
        (baseline,) = await _revisions(db_session, item.id)
        assert baseline.content_fingerprint == DIGEST
        assert baseline.spec_fingerprint == "spec1:x"
        assert baseline.processor_version == "0.19.7+1"
        change.assert_not_awaited()
        (event,) = await _audits(db_session, EventType.CHECK_SNAPSHOT_CREATED)
        assert event.payload["source"] == "processor"

    async def test_a_200_without_validators_clears_the_stored_pair(self, db_session, monkeypatch):
        # Always an overwrite: the pair must describe the latest 200, so an
        # origin that stopped sending one must not leave the old one replayable.
        row = await _decisive(
            db_session,
            item_over={"etag": 'W/"stale"', "last_modified": "Mon, 11 Aug 2026 10:00:00 GMT"},
            **_completed(),
        )
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)

        await apply_process_fact(row.command_id)

        item = await self._item(db_session, row)
        assert item.etag is None
        assert item.last_modified is None

    async def test_the_first_success_republishes_status_once(self, db_session, monkeypatch):
        # #264: a health transition defers a watch-status republish.
        row = await _decisive(db_session, **_completed())
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)
        republish = AsyncMock()
        monkeypatch.setattr(fc_mod, "defer_status_republish", republish)

        await apply_process_fact(row.command_id)

        assert (await self._item(db_session, row)).health_status == WatchHealthStatus.OK
        republish.assert_awaited_once()

    async def test_an_unchanged_answer_names_a_renewal_in_its_audit(self, db_session, monkeypatch):
        """#293: the audit is the operator-visible surface, so a renewal is
        legible there; an ordinary unchanged check leaves the key absent."""
        renewed = await _decisive(db_session, **_completed())
        plain = await _decisive(db_session, **_completed())
        for row, revisions in ((renewed, 2), (plain, 1)):
            item = await self._item(db_session, row)
            item.processor_version = "0.19.7+1"
            for age in range(revisions):
                db_session.add(
                    ChangeRevision(
                        watched_item_id=item.id,
                        content_fingerprint=OTHER_DIGEST if age else DIGEST,
                        captured_at=datetime.now(UTC) - timedelta(days=1 + age),
                        content_size_bytes=10,
                        schema_version=1,
                    )
                )
        await db_session.flush()
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)

        assert (await apply_process_fact(renewed.command_id))["renewal_enqueued"] is True
        assert (await apply_process_fact(plain.command_id))["renewal_enqueued"] is False

        events = {
            e.payload["watched_item_id"]: e.payload
            for e in await _audits(db_session, EventType.CHECK_NO_CHANGE)
        }
        assert events[str(renewed.watched_item_id)]["renewal_enqueued"] is True
        assert "renewal_enqueued" not in events[str(plain.watched_item_id)]

    async def test_a_new_digest_is_a_change(self, db_session, monkeypatch):
        row = await _decisive(db_session, **_completed(output_digest=OTHER_DIGEST))
        item = await self._item(db_session, row)
        db_session.add(
            ChangeRevision(
                watched_item_id=item.id,
                content_fingerprint=DIGEST,
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
                content_fingerprint=DIGEST,
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
        assert item.last_observed_at is None  # #258: nothing was verified
        assert item.etag is None  # #269: the next fetch is in full
        assert await _revisions(db_session, item.id) == []
        assert len(await _audits(db_session, EventType.CHECK_EXTRACTION_FAILED)) == 1

    async def test_a_persistent_failure_re_notifies_without_republishing(
        self, db_session, monkeypatch
    ):
        """#71 on the path production runs: a processor-decided failure of an
        item already in ERROR past the window reminds, and is no transition."""
        monkeypatch.delenv(ERROR_RENOTIFY_INTERVAL_ENV, raising=False)
        told = datetime.now(UTC) - timedelta(hours=25)
        row = await _decisive(
            db_session,
            specs=(SPEC_A,),
            item_over={"health_status": WatchHealthStatus.ERROR, "last_error_notified_at": told},
            **_empty(),
        )
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)
        dispatch = AsyncMock(return_value=0)
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", dispatch)
        republish = AsyncMock()
        monkeypatch.setattr(fc_mod, "defer_status_republish", republish)

        await apply_process_fact(row.command_id)

        event = dispatch.await_args.kwargs["event"]
        assert event.event_type == WatchEventType.WATCH_ERROR
        assert event.metadata["renotify"] is True
        assert event.metadata["previously_notified_at"] == format_utc_iso(told)
        assert (await self._item(db_session, row)).last_error_notified_at == event.occurred_at
        republish.assert_not_awaited()

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

    async def _unreadable_with_a_replayable_pair(
        self, db_session, monkeypatch, **fields
    ) -> ProcessCommand:
        """#361: the blob leg stamped the item as fetched; the processor then
        could not read the bytes. The pair from the last success still matches
        its key, and the gate is on for this item alone. ``fields`` go to the
        process row — the #362 cap test sets its ``reissue_count``."""
        row = await _decisive(
            db_session,
            status=ProcessCommandStatus.FAILED,
            fact_at=datetime.now(UTC),
            failure_reason="input_unreadable",
            failure_detail="blob gone",
            **fields,
        )
        item = await self._item(db_session, row)
        item.etag = 'W/"old"'
        item.last_modified = "Wed, 13 Aug 2026 10:00:00 GMT"
        item.processor_version = "0.19.7+1"
        item.validator_source_key = validator_source_key(
            effective_url=item.effective_url, source_specs=item.source_specs, generation="0.19.7+1"
        )
        # Real clock: the re-issue resolves validators against datetime.now.
        item.last_full_fetch_at = datetime.now(UTC)
        item.blob_expires_at = item.last_full_fetch_at + timedelta(days=7)
        await db_session.flush()
        monkeypatch.setenv(CONDITIONAL_GET_ENV, str(item.id))
        # Not vacuous: an unforced occasion would replay this pair.
        assert replayable_validators(item, now=datetime.now(UTC)) == (
            'W/"old"',
            "Wed, 13 Aug 2026 10:00:00 GMT",
        )
        _wire(db_session, monkeypatch)
        _quiet(monkeypatch)
        return row

    async def test_input_unreadable_re_fetches_without_validators(self, db_session, monkeypatch):
        # The stamp vouches for bytes nobody could read: a 304 would close the
        # check with no bytes and no #293 renewal.
        row = await self._unreadable_with_a_replayable_pair(db_session, monkeypatch)

        result = await apply_process_fact(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        reissued = await db_session.get(FetchCommand, result["reissued"])
        assert reissued.forced_full_fetch is True
        assert reissued.request_etag is None
        assert reissued.request_last_modified is None

    async def test_the_forced_re_fetch_stays_forced_down_its_lineage(self, db_session, monkeypatch):
        # A lineage that has already lost its bytes: the reaper's re-issue of
        # the forced re-fetch inherits the intent (CR-1) rather than replaying.
        row = await self._unreadable_with_a_replayable_pair(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()
        result = await apply_process_fact(row.command_id, bus_client=client)
        reissued = await db_session.get(FetchCommand, result["reissued"])
        reissued.status = FetchCommandStatus.EXPIRED

        again_id = await reissue_fetch_command(
            db_session, await self._item(db_session, row), reissued, client
        )

        again = await db_session.get(FetchCommand, again_id)
        assert again.forced_full_fetch is True
        assert again.request_etag is None

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

    async def test_input_unreadable_at_the_cap_forgets_the_pair(self, db_session, monkeypatch):
        # #362: the item re-enters normal scheduling unforced, and the stamp
        # names bytes nobody could read — a replayed pair's 304 would flip
        # ERROR to OK with no bytes and no #293 renewal.
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "1")
        row = await self._unreadable_with_a_replayable_pair(
            db_session, monkeypatch, reissue_count=1
        )

        result = await apply_process_fact(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        assert result["error"] == "blob_unreadable"
        item = await self._item(db_session, row)
        # What committed, not the identity map: the clear must precede
        # record_check_failure's commit, and nothing on this path commits after.
        await db_session.refresh(item)
        assert (item.etag, item.last_modified, item.validator_source_key) == (None, None, None)
        nxt = await create_fetch_command(db_session, item, now=datetime.now(UTC))
        assert (nxt.request_etag, nxt.request_last_modified) == (None, None)

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

    async def test_a_closed_checks_lineage_just_expires(self, db_session, monkeypatch):
        monkeypatch.setenv(PROCESS_COMMAND_HARD_LIMIT_ENV, "7200")
        row = await _command(
            db_session,
            issued_at=datetime.now(UTC) - timedelta(hours=3),
            status=ProcessCommandStatus.PENDING_PUBLISH,
        )

        await reap_process_commands(session=db_session, bus_client=fakeredis.FakeAsyncRedis())

        assert row.status == ProcessCommandStatus.EXPIRED
        fetch = await db_session.get(FetchCommand, row.fetch_command_id)
        assert fetch.status == FetchCommandStatus.SUCCEEDED

    async def test_a_recent_unpublished_command_is_left_to_the_sweep(self, db_session, monkeypatch):
        row = await _decisive(db_session, status=ProcessCommandStatus.PENDING_PUBLISH)

        result = await reap_process_commands(
            session=db_session, bus_client=fakeredis.FakeAsyncRedis()
        )

        assert result["hard_limited"] == 0
        assert row.status == ProcessCommandStatus.PENDING_PUBLISH


class TestProcessorEndToEnd:
    """Blob applied → command → fake processor → fact → the check decided (#326).

    The fake answers with a digest of its own choosing: Watcher never computes
    one, so the only claim is that the reported digest becomes the fingerprint.
    """

    SPECS = [{"extraction": {"algorithm": "css", "selector": "main"}, "schema_version": 1}]

    async def _occasion(self, db_session, wi, client, **fact):
        fetch = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        fetch.status = FetchCommandStatus.IN_FLIGHT
        fetch.published_at = fetch.fact_at = datetime.now(UTC)
        fetch.blob_uri = "gs://co-gcs-blobs/blobs/never-read.bin"
        fetch.content_fingerprint = RAW_DIGEST
        fetch.media_type = "text/html"
        for key, value in fact.items():
            setattr(fetch, key, value)
        await db_session.flush()
        result = await apply_fetch_blob(fetch.command_id, bus_client=client)
        assert "processing" in result
        return fetch

    def _answer(self, command, text: bytes):
        event = ProcessingCompleteEmit(
            occurred_at=datetime.now(UTC),
            command_id=command.command_id,
            info_source_id=command.info_source_id,
            empty=False,
            output_digest=f"sha256:{hashlib.sha256(text).hexdigest()}",
            output_uri="gs://co-gcs-processor/blobs/x.bin",
            output_size_bytes=len(text),
            output_media_type=CANONICAL_TEXT_MEDIA_TYPE,
            spec_schema_version=1,
            processor_version="0.19.7+1",
            spec_fingerprint="spec1:sha256:" + "cd" * 32,
        )
        return from_wire(to_wire(event), topic=streams.CONTENT_DERIVED, message_id="1-1")

    async def _latest_command(self, client):
        ((_, fields),) = (await client.xrevrange(streams.CONTENT_PROCESS, count=1))[:1]
        frame = {k.decode(): v.decode() for k, v in fields.items()}
        return from_wire(frame, topic=streams.CONTENT_PROCESS).payload

    async def _decide(self, db_session, client, text: bytes):
        async def _apply_now(command_id):
            await apply_process_fact(command_id, bus_client=client)

        command = await self._latest_command(client)
        await process_derived_message(db_session, self._answer(command, text), defer=_apply_now)

    def _wired(self, db_session, monkeypatch):
        _wire(db_session, monkeypatch)
        monkeypatch.setattr(fc_mod, "get_session_factory", pc_mod.get_session_factory)
        return _quiet(monkeypatch)

    async def test_baseline_then_a_change(self, db_session, monkeypatch):
        change = self._wired(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()
        wi = await make_watched_item(
            db_session, primary_url="https://lcb.wa.gov/boardmeetings", source_specs=self.SPECS
        )

        for text in (b"Board meets Tuesday", b"Board meets Thursday"):
            fetch = await self._occasion(db_session, wi, client)
            await self._decide(db_session, client, text)
            assert fetch.status == FetchCommandStatus.SUCCEEDED

        _baseline, changed = await _revisions(db_session, wi.id)
        # The processor's digest is the stored fingerprint, byte for byte.
        assert changed.content_fingerprint == (
            f"sha256:{hashlib.sha256(b'Board meets Thursday').hexdigest()}"
        )
        assert wi.health_status == WatchHealthStatus.OK
        change.assert_awaited_once()
        assert await get_open_command(db_session, wi.id) is None

    async def test_a_probe_resolves_and_keys_the_pair_to_the_final_url(
        self, db_session, monkeypatch
    ):
        """CR-4 across both legs: the blob leg moves ``effective_url``, the
        derived leg stores the pair — keyed to where the bytes came from."""
        self._wired(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()
        wi = await make_watched_item(
            db_session, primary_url="https://lcb.wa.gov/notices", source_specs=self.SPECS
        )
        wi.health_status = WatchHealthStatus.PROBING
        await db_session.flush()

        await self._occasion(
            db_session, wi, client, etag='W/"v2"', final_url="https://www.lcb.wa.gov/notices"
        )
        await self._decide(db_session, client, b"Notices")

        assert wi.effective_url == "https://www.lcb.wa.gov/notices"
        assert wi.health_status == WatchHealthStatus.OK
        assert wi.etag == 'W/"v2"'
        assert wi.validator_source_key == validator_source_key(
            effective_url="https://www.lcb.wa.gov/notices",
            source_specs=self.SPECS,
            generation="0.19.7+1",
        )
