"""Tests for apply_fetch_blob / apply_fetch_failure / reap_fetch_commands (#241 step 2).

The apply path must leave the SAME bookkeeping the retired local fetch path
left — health, ``last_checked_at``, check audits, error surfacing — and must be
safe under the bus's actual delivery semantics: duplicates (status guard), no
ordering (supersession guard). A blob fact never decides a check: it hands the
check to the processor, whose derived fact closes it (#326, #350).
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from sqlalchemy import select

import src.workers.fetch_commands as fc_mod
import src.workers.process_issue as pi_mod
from src.core.fetch_commands import create_fetch_command, get_open_command
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.fetch_command import (
    INVALID_REQUEST_OPTIONS_REASON,
    OPEN_STATUSES,
    FetchCommand,
    FetchCommandStatus,
)
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.process_command import ProcessCommand, ProcessCommandStatus
from src.core.models.watched_item import WatchHealthStatus
from src.core.notifications.events import WatchEventType
from src.core.notifications.renotify import ERROR_RENOTIFY_INTERVAL_ENV
from src.core.utils import format_utc_iso
from src.core.validators import CONDITIONAL_GET_ENV, validator_source_key
from src.workers.fetch_commands import (
    apply_fetch_blob,
    apply_fetch_failure,
    apply_fetch_not_modified,
    reap_fetch_commands,
    reissue_fetch_command,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 6, 17, 30, 0, tzinfo=UTC)
SPECS = [{"extraction": {"algorithm": "full_page"}, "schema_version": 1}]


def _mock_session_factory(db_session):
    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


def _wire(db_session, monkeypatch) -> None:
    monkeypatch.setattr(fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session))


async def _row_with_fact(db_session, *, specs=SPECS, **fact_over):
    """An in-flight command whose blob fact has landed. Nothing exists at the
    URI: Watcher never reads a raw blob, and a read would raise."""
    wi = await make_watched_item(
        db_session, primary_url="https://lcb.wa.gov/notices", source_specs=specs
    )
    row = await create_fetch_command(db_session, wi, now=NOW)
    row.status = FetchCommandStatus.IN_FLIGHT
    row.published_at = NOW
    row.blob_uri = "gs://co-gcs-blobs/blobs/never-read.bin"
    row.fact_at = NOW
    row.content_fingerprint = "ab" * 32
    for key, value in fact_over.items():
        setattr(row, key, value)
    await db_session.flush()
    return wi, row


async def _audit_events(db_session, event_type) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.event_type == event_type)
    return list((await db_session.execute(stmt)).scalars().all())


async def _process_rows(db_session, fetch_row) -> list[ProcessCommand]:
    stmt = select(ProcessCommand).where(ProcessCommand.fetch_command_id == fetch_row.command_id)
    return list((await db_session.execute(stmt)).scalars().all())


class TestApplyFetchBlob:
    """The blob fact no longer decides (#326): Watcher reads no raw blob and
    extracts nothing. The apply records what the fetch fact itself says, moves
    the row to ``PROCESSING`` — still open, so nothing is issued behind it — and
    hands spec[0] to the processor. The derived fact closes the check
    (``apply_process_fact``).
    """

    async def test_hands_spec_zero_to_the_processor(self, db_session, monkeypatch):
        wi, row = await _row_with_fact(db_session, reissue_count=1)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_fetch_blob(row.command_id, bus_client=client)

        (command,) = await _process_rows(db_session, row)
        assert result == {"processing": command.command_id}
        assert row.status == FetchCommandStatus.PROCESSING
        assert row.applied_at is None
        assert command.status == ProcessCommandStatus.IN_FLIGHT
        assert command.spec_index == 0
        assert command.input_digest == row.content_fingerprint
        assert command.reissue_count == 1  # the lineage spans both legs
        assert await client.xlen("content.process") == 1

    async def test_the_check_stays_open_until_the_derived_fact(self, db_session, monkeypatch):
        wi, row = await _row_with_fact(db_session)
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        assert await get_open_command(db_session, wi.id) is row
        # Health and the check clock belong to the outcome, which is not in yet.
        assert wi.health_status == WatchHealthStatus.UNKNOWN
        assert wi.last_checked_at is None
        assert wi.last_observed_at is None
        assert await _audit_events(db_session, EventType.CHECK_SNAPSHOT_CREATED) == []

    async def test_records_the_fetch_but_not_yet_the_validators(self, db_session, monkeypatch):
        # Bytes arrived — that is the fetch fact's to say. The pair is stored
        # with the outcome it vouches for, keyed to the extractor that produced
        # it, when the derived fact closes the row.
        wi, row = await _row_with_fact(
            db_session, etag='"v2"', blob_expires_at=NOW + timedelta(days=7)
        )
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        assert wi.last_full_fetch_at is not None
        assert wi.blob_expires_at == NOW + timedelta(days=7)
        assert wi.etag is None
        assert wi.validator_source_key is None

    async def test_a_duplicate_defer_is_a_no_op(self, db_session, monkeypatch):
        _, row = await _row_with_fact(db_session)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        await apply_fetch_blob(row.command_id, bus_client=client)
        second = await apply_fetch_blob(row.command_id, bus_client=client)

        assert second == {"skipped": True, "reason": "status_processing"}
        assert len(await _process_rows(db_session, row)) == 1

    async def test_out_of_order_apply_is_superseded(self, db_session, monkeypatch):
        # A reaper re-issue racing a recovered original: the newer command
        # applied first; the older must not flap the fingerprint A→B→A, nor
        # overwrite the validators the newer one stored (MUST-5).
        wi, older = await _row_with_fact(db_session, etag='W/"old"')
        newer = await create_fetch_command(
            db_session, wi, now=NOW + timedelta(minutes=5), intent_id=older.intent_id
        )
        newer.status = FetchCommandStatus.SUCCEEDED
        newer.applied_at = NOW + timedelta(minutes=6)
        wi.etag = 'W/"new"'
        await db_session.flush()
        _wire(db_session, monkeypatch)

        result = await apply_fetch_blob(older.command_id, bus_client=fakeredis.FakeAsyncRedis())

        assert result == {"skipped": True, "reason": "superseded"}
        assert older.status == FetchCommandStatus.SUPERSEDED
        assert await _process_rows(db_session, older) == []
        assert wi.etag == 'W/"new"'

    async def test_seeds_media_type_from_raw_header_once(self, db_session, monkeypatch):
        wi, row = await _row_with_fact(
            db_session, content_type_raw="application/pdf; charset=binary"
        )
        assert wi.content_media_type is None
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        await apply_fetch_blob(row.command_id, bus_client=client)
        assert wi.content_media_type == "application/pdf; charset=binary"
        # The command dispatches on the seeded essence.
        (command,) = await _process_rows(db_session, row)
        assert command.media_type == "application/pdf"

        # Never clobbered on a later apply.
        wi.content_media_type = "operator/override"
        another = await create_fetch_command(db_session, wi, now=NOW + timedelta(minutes=9))
        another.status = FetchCommandStatus.IN_FLIGHT
        another.blob_uri = row.blob_uri
        another.content_fingerprint = "cd" * 32
        another.content_type_raw = "text/html"
        await db_session.flush()
        await apply_fetch_blob(another.command_id, bus_client=client)
        assert wi.content_media_type == "operator/override"

    async def test_absent_raw_header_leaves_media_type_unset(self, db_session, monkeypatch):
        """A fact with no ``content_type_raw`` must not write an empty seed (#168)."""
        wi, row = await _row_with_fact(db_session, content_type_raw=None)
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        assert wi.content_media_type is None

    async def test_a_specless_item_fails_at_once(self, db_session, monkeypatch):
        # Nothing to send (#260): the extraction-failure path, without a
        # round trip — and the row closes, so the gate lifts.
        wi, row = await _row_with_fact(db_session, specs=[])
        _wire(db_session, monkeypatch)
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", AsyncMock(return_value=0))

        result = await apply_fetch_blob(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        assert result == {"error": "extraction_failed"}
        assert row.status == FetchCommandStatus.FAILED
        assert row.failure_reason == "processing_failed"
        assert "source_specs" in row.failure_detail
        assert wi.health_status == WatchHealthStatus.ERROR
        assert wi.last_observed_at is None  # #258: nothing was verified
        assert await _process_rows(db_session, row) == []
        (event,) = await _audit_events(db_session, EventType.CHECK_EXTRACTION_FAILED)
        assert event.payload["reason"] == "processing_failed"

    async def test_a_publish_failure_leaves_both_rows_for_the_sweep(self, db_session, monkeypatch):
        _, row = await _row_with_fact(db_session)
        _wire(db_session, monkeypatch)

        async def _down(*args, **kwargs):
            raise ConnectionError("broker down")

        monkeypatch.setattr(pi_mod, "publish_process_command", _down)

        await apply_fetch_blob(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        (command,) = await _process_rows(db_session, row)
        assert command.status == ProcessCommandStatus.PENDING_PUBLISH
        assert row.status == FetchCommandStatus.PROCESSING

    async def test_redirect_divergence_is_audited(self, db_session, monkeypatch):
        wi, row = await _row_with_fact(db_session, final_url="https://lcb.wa.gov/moved-here")
        original_url = wi.effective_url
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        events = await _audit_events(db_session, EventType.CHECK_REDIRECT_OBSERVED)
        assert len(events) == 1
        assert events[0].payload["final_url"] == "https://lcb.wa.gov/moved-here"
        # Non-PROBING items keep the audit-only behaviour: Archiver stays
        # authoritative for effective_url after the probe phase.
        assert wi.effective_url == original_url

    async def test_final_url_resolves_a_probing_item(self, db_session, monkeypatch):
        """#241 step 3: a PROBING item's first fact is its probe. PROBING
        clears to OK with the derived fact, like any other outcome."""
        wi, row = await _row_with_fact(db_session, final_url="https://www.lcb.wa.gov/notices")
        wi.health_status = WatchHealthStatus.PROBING
        await db_session.flush()
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, bus_client=fakeredis.FakeAsyncRedis())

        assert wi.effective_url == "https://www.lcb.wa.gov/notices"
        assert wi.domain_name == "www.lcb.wa.gov"
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "www.lcb.wa.gov"))
        ).scalar_one_or_none()
        assert domain is not None  # ensure_domain upserted the new host


class TestApplyFetchFailure:
    async def test_surfaces_error_health_and_audit(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = "http_status"
        row.status_code = 404
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await apply_fetch_failure(row.command_id)

        assert result == {"applied": True, "reason": "http_status"}
        assert wi.health_status == WatchHealthStatus.ERROR
        assert wi.last_checked_at is not None
        assert row.applied_at is not None
        events = await _audit_events(db_session, EventType.CHECK_FETCH_FAILED)
        assert len(events) == 1
        assert events[0].payload["reason"] == "http_status"
        assert events[0].payload["status_code"] == 404

    async def test_idempotent_once_applied(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = "http_status"
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        await apply_fetch_failure(row.command_id)
        second = await apply_fetch_failure(row.command_id)

        assert second == {"skipped": True, "reason": "already_applied"}
        assert len(await _audit_events(db_session, EventType.CHECK_FETCH_FAILED)) == 1

    async def test_an_unrecognised_reason_journals_generically(self, db_session, monkeypatch):
        """A token this code has never heard of takes the plain failure path.

        The apply branches on exactly one token (``invalid_request_options``,
        which clears validators). Everything else is journalled verbatim, so a
        producer adding a reason — CannObserv/replicator#95's destination guard
        is the live one (watcher#304) — needs no change here. Pinned because the
        failure mode is silent: a token accidentally routed through the
        validator-clearing branch would buy a full re-fetch on every occurrence.
        """
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.etag = 'W/"v7"'
        wi.last_modified = "Wed, 13 Aug 2026 10:00:00 GMT"
        wi.validator_source_key = "sha256:whatever"
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = "token_from_a_newer_producer"
        row.failure_detail = "refused before the request was issued"
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", AsyncMock(return_value=0))

        result = await apply_fetch_failure(row.command_id)

        assert result == {"applied": True, "reason": "token_from_a_newer_producer"}
        assert wi.health_status == WatchHealthStatus.ERROR
        assert wi.last_checked_at is not None
        events = await _audit_events(db_session, EventType.CHECK_FETCH_FAILED)
        assert len(events) == 1
        assert events[0].payload["reason"] == "token_from_a_newer_producer"
        # Absent status_code stays absent rather than arriving as a null key:
        # the guard-style refusals never reach an origin, so there is none.
        assert "status_code" not in events[0].payload
        # Says nothing about our validators — only the one token may clear them.
        assert wi.etag == 'W/"v7"'
        assert wi.last_modified == "Wed, 13 Aug 2026 10:00:00 GMT"
        assert wi.validator_source_key == "sha256:whatever"


class TestApplyFetchNotModified:
    """#249 part 1: a 304 is a successful check that found no change.

    The trap this closes: routed to ``apply_fetch_failure`` it would set ERROR
    health, write ``CHECK_FETCH_FAILED``, and fire one ``WATCH_ERROR`` — on
    *every* successful no-change check, for the most useful answer an origin
    can give.
    """

    async def _not_modified_row(self, db_session, **wi_kwargs):
        wi = await make_watched_item(
            db_session, primary_url="https://lcb.wa.gov/notices", **wi_kwargs
        )
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.NOT_MODIFIED
        row.status_code = 304
        row.fact_at = NOW
        await db_session.flush()
        return wi, row

    def _spy_dispatch(self, monkeypatch) -> AsyncMock:
        spy = AsyncMock(return_value=0)
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", spy)
        return spy

    async def test_records_an_unchanged_check_with_ok_health(self, db_session, monkeypatch):
        wi, row = await self._not_modified_row(db_session)
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        dispatch = self._spy_dispatch(monkeypatch)

        result = await apply_fetch_not_modified(row.command_id)

        assert result == {"applied": True, "not_modified": True}
        assert wi.health_status == WatchHealthStatus.OK
        assert wi.last_checked_at is not None
        # A 304 IS an observation: the origin asserted its bytes are current.
        assert wi.last_observed_at is not None
        assert row.applied_at is not None
        assert row.status == FetchCommandStatus.NOT_MODIFIED
        # No bytes → nothing goes to the processor.
        assert (await db_session.execute(select(ProcessCommand))).scalars().all() == []
        # …and nothing reached the outbox. Trivial before #293, when "unchanged"
        # meant "no row" everywhere; now a full-fetch cache hit CAN enqueue one,
        # so this is a live distinction rather than a restatement of the line
        # above (CR 5). Pinned on the outcome, not on the stub.
        queued = (await db_session.execute(select(PendingArchiverSync))).scalars().all()
        assert queued == []
        # No WATCH_ERROR, and no notification at all on a steady OK item.
        assert dispatch.await_count == 0

    async def test_audits_as_checked_unchanged_never_as_a_fetch_failure(
        self, db_session, monkeypatch
    ):
        _, row = await self._not_modified_row(db_session)
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        self._spy_dispatch(monkeypatch)

        await apply_fetch_not_modified(row.command_id)

        assert await _audit_events(db_session, EventType.CHECK_FETCH_FAILED) == []
        assert await _audit_events(db_session, EventType.CHECK_SNAPSHOT_CREATED) == []
        events = await _audit_events(db_session, EventType.CHECK_NO_CHANGE)
        assert len(events) == 1
        assert events[0].payload["changed"] is False
        assert events[0].payload["baseline"] is False
        # Distinguishable from an unchanged *extraction* in the audit trail.
        assert events[0].payload["source"] == "not_modified"

    async def test_error_item_recovers(self, db_session, monkeypatch):
        wi, row = await self._not_modified_row(db_session)
        wi.health_status = WatchHealthStatus.ERROR
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        dispatch = self._spy_dispatch(monkeypatch)

        await apply_fetch_not_modified(row.command_id)

        assert wi.health_status == WatchHealthStatus.OK
        assert dispatch.await_count == 1
        event = dispatch.await_args.kwargs["event"]
        assert event.event_type == WatchEventType.WATCH_RECOVERED

    async def test_idempotent_once_applied(self, db_session, monkeypatch):
        _, row = await self._not_modified_row(db_session)
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        self._spy_dispatch(monkeypatch)

        await apply_fetch_not_modified(row.command_id)
        second = await apply_fetch_not_modified(row.command_id)

        assert second == {"skipped": True, "reason": "already_applied"}
        assert len(await _audit_events(db_session, EventType.CHECK_NO_CHANGE)) == 1

    async def test_unknown_command_is_a_noop(self, db_session, monkeypatch):
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        result = await apply_fetch_not_modified("01UNKNOWNCOMMANDIDXXXXXXXX")
        assert result == {"skipped": True, "reason": "unknown_command"}

    async def test_deleted_watched_item_is_a_noop(self, db_session, monkeypatch):
        wi, row = await self._not_modified_row(db_session)
        await db_session.delete(wi)
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        result = await apply_fetch_not_modified(row.command_id)
        assert result == {"skipped": True, "reason": "watched_item_gone"}


class TestNotModifiedStatusContract:
    """The two status readers the new member has to satisfy (#249)."""

    async def test_not_modified_is_not_an_open_command(self, db_session):
        # The scheduling gate: a 304 closes the command, so the item must be
        # free to be checked again on its next tick.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.NOT_MODIFIED
        await db_session.flush()

        assert FetchCommandStatus.NOT_MODIFIED not in OPEN_STATUSES
        assert await get_open_command(db_session, wi.id) is None

    async def test_reaper_leaves_a_not_modified_row_alone(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.NOT_MODIFIED
        row.published_at = datetime.now(UTC) - timedelta(minutes=60)
        await db_session.flush()

        client = fakeredis.FakeAsyncRedis()
        result = await reap_fetch_commands(session=db_session, bus_client=client)

        assert result == {"reissued": 0, "capped": 0, "reapplied": 0}
        assert row.status == FetchCommandStatus.NOT_MODIFIED


class TestReapFetchCommands:
    async def _stalled_row(self, db_session, *, age_minutes=60, reissue_count=0):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW, reissue_count=reissue_count)
        row.status = FetchCommandStatus.IN_FLIGHT
        row.published_at = datetime.now(UTC) - timedelta(minutes=age_minutes)
        await db_session.flush()
        return wi, row

    async def test_stalled_command_is_expired_and_reissued(self, db_session):
        wi, row = await self._stalled_row(db_session)
        client = fakeredis.FakeAsyncRedis()

        result = await reap_fetch_commands(session=db_session, bus_client=client)

        assert result == {"reissued": 1, "capped": 0, "reapplied": 0}
        assert row.status == FetchCommandStatus.EXPIRED
        rows = list(
            (
                await db_session.execute(
                    select(FetchCommand).where(FetchCommand.intent_id == row.intent_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        new_row = next(r for r in rows if r.command_id != row.command_id)
        assert new_row.reissue_count == 1
        assert new_row.status == FetchCommandStatus.IN_FLIGHT

    async def test_reissue_cap_fails_the_intent_with_error_health(self, db_session):
        wi, row = await self._stalled_row(db_session, reissue_count=3)
        client = fakeredis.FakeAsyncRedis()

        result = await reap_fetch_commands(session=db_session, bus_client=client)

        assert result == {"reissued": 0, "capped": 1, "reapplied": 0}
        assert row.status == FetchCommandStatus.FAILED
        assert row.failure_reason == "fetch_timeout"
        assert wi.health_status == WatchHealthStatus.ERROR
        events = await _audit_events(db_session, EventType.CHECK_FETCH_FAILED)
        assert events and events[0].payload["reason"] == "fetch_timeout"
        # The gate lifts: no open command remains, so scheduling resumes.
        assert await client.xlen("content.fetch") == 0

    async def test_fresh_rows_and_fresh_facts_are_left_alone(self, db_session):
        _, fresh = await self._stalled_row(db_session, age_minutes=1)
        _, facted = await self._stalled_row(db_session, age_minutes=60)
        facted.fact_at = datetime.now(UTC)  # recent fact — apply presumably queued
        await db_session.flush()
        client = fakeredis.FakeAsyncRedis()

        result = await reap_fetch_commands(session=db_session, bus_client=client)

        assert result == {"reissued": 0, "capped": 0, "reapplied": 0}
        assert fresh.status == FetchCommandStatus.IN_FLIGHT
        assert facted.status == FetchCommandStatus.IN_FLIGHT

    async def test_stale_fact_with_blob_resurrects_the_apply(self, db_session, monkeypatch):
        # CR-2: a fact whose apply job died must not shield the row forever —
        # the reaper re-defers the apply (bytes exist; refetching would waste
        # an origin request) instead of re-issuing.
        _, row = await self._stalled_row(db_session, age_minutes=60)
        row.fact_at = datetime.now(UTC) - timedelta(minutes=60)
        row.blob_uri = "file:///var/lib/replicator/blobs/ab/cd/abcd.bin"
        await db_session.flush()
        deferred: list[str] = []

        async def _spy(command_id: str) -> None:
            deferred.append(command_id)

        monkeypatch.setattr(fc_mod, "_defer_reapply", _spy)
        client = fakeredis.FakeAsyncRedis()

        result = await reap_fetch_commands(session=db_session, bus_client=client)

        assert result == {"reissued": 0, "capped": 0, "reapplied": 1}
        assert deferred == [row.command_id]
        assert row.status == FetchCommandStatus.IN_FLIGHT  # still open; gate holds
        assert row.fact_at is not None and row.fact_at > NOW - timedelta(days=1)
        assert await client.xlen("content.fetch") == 0  # no wasteful refetch


class TestObservationFreshness:
    """#264: ``last_observed_at`` is provenance — "content was verified
    current" — distinct from ``last_checked_at``, the anti-thrash stamp that
    advances on every outcome (#168). The success side is the derived leg's
    (``tests/workers/test_process_commands.py``)."""

    async def test_failure_advances_last_checked_at_but_not_last_observed_at(
        self, db_session, monkeypatch
    ):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = "http_status"
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        await apply_fetch_failure(row.command_id)

        assert wi.last_checked_at is not None
        assert wi.last_observed_at is None


class TestStatusRepublishOnTransition:
    """#264: level-not-edge publishing — a health transition defers a
    watch-status republish; a steady state never does (the periodic tick
    carries it), keeping the stream off the activity-rate cost curve. The
    success transition is the derived leg's."""

    def _spy(self, monkeypatch) -> AsyncMock:
        spy = AsyncMock()
        monkeypatch.setattr(fc_mod, "defer_status_republish", spy)
        return spy

    async def test_failure_transition_defers_once(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.health_status = WatchHealthStatus.OK
        first = await create_fetch_command(db_session, wi, now=NOW)
        first.status = FetchCommandStatus.FAILED
        first.failure_reason = "http_status"
        second = await create_fetch_command(db_session, wi, now=NOW)
        second.status = FetchCommandStatus.FAILED
        second.failure_reason = "http_status"
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        spy = self._spy(monkeypatch)

        await apply_fetch_failure(first.command_id)
        await apply_fetch_failure(second.command_id)

        assert wi.health_status == WatchHealthStatus.ERROR
        assert spy.await_count == 1  # only the OK -> ERROR transition


class TestErrorRenotify:
    """#71: a persistent ERROR re-sends ``WATCH_ERROR`` once per
    ``WATCHER_ERROR_RENOTIFY_INTERVAL``, stamping ``last_error_notified_at`` on
    every dispatch and clearing it on recovery. A repeat is not a health
    transition, so it never republishes watch-status (#264)."""

    @pytest.fixture(autouse=True)
    def _wired(self, db_session, monkeypatch):
        monkeypatch.delenv(ERROR_RENOTIFY_INTERVAL_ENV, raising=False)
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        self.dispatch = AsyncMock(return_value=0)
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", self.dispatch)
        self.republish = AsyncMock()
        monkeypatch.setattr(fc_mod, "defer_status_republish", self.republish)

    async def _item(self, db_session, *, health, notified_ago=None):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.health_status = health
        if notified_ago is not None:
            wi.last_error_notified_at = datetime.now(UTC) - notified_ago
        await db_session.flush()
        return wi

    async def _fail(self, db_session, wi, *, reason="http_status", status_code=503):
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = reason
        row.status_code = status_code
        await db_session.flush()
        await apply_fetch_failure(row.command_id)
        return row

    def _events(self) -> list:
        return [c.kwargs["event"] for c in self.dispatch.await_args_list]

    async def test_first_error_notifies_and_stamps(self, db_session):
        wi = await self._item(db_session, health=WatchHealthStatus.OK)

        await self._fail(db_session, wi)

        [event] = self._events()
        assert event.event_type == WatchEventType.WATCH_ERROR
        assert event.metadata["renotify"] is False
        assert event.metadata["previously_notified_at"] == ""
        assert wi.last_error_notified_at == event.occurred_at
        # The window measures from the check clock (CR 4): next due is
        # last_checked_at + cadence, so a 1d cadence never misses a 24h window.
        assert wi.last_error_notified_at == wi.last_checked_at
        assert self.republish.await_count == 1  # the OK -> ERROR transition

    async def test_silent_inside_the_window(self, db_session):
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=1)
        )
        stamp = wi.last_error_notified_at

        await self._fail(db_session, wi)

        assert self._events() == []
        assert wi.last_error_notified_at == stamp
        assert self.republish.await_count == 0

    async def test_renotifies_once_the_window_has_elapsed(self, db_session):
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=25)
        )
        previous = wi.last_error_notified_at

        await self._fail(db_session, wi)

        [event] = self._events()
        assert event.event_type == WatchEventType.WATCH_ERROR
        assert event.metadata["renotify"] is True
        assert event.metadata["previously_notified_at"] == format_utc_iso(previous)
        assert event.metadata["status_code"] == 503
        assert wi.last_error_notified_at == event.occurred_at
        assert event.occurred_at > previous
        # Not a health transition: the level signal did not change (#264).
        assert self.republish.await_count == 0
        assert wi.health_status == WatchHealthStatus.ERROR

    async def test_the_interval_is_the_env_knob(self, db_session, monkeypatch):
        monkeypatch.setenv(ERROR_RENOTIFY_INTERVAL_ENV, "1h")
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=2)
        )

        await self._fail(db_session, wi)

        assert [e.metadata["renotify"] for e in self._events()] == [True]

    async def test_an_error_item_with_no_stamp_is_due(self, db_session):
        """ERROR with no record of telling anyone: tell them, rather than stay
        silent for ever — the migration backfills existing rows, so this is a
        guard, not a deploy-time burst."""
        wi = await self._item(db_session, health=WatchHealthStatus.ERROR)

        await self._fail(db_session, wi)

        [event] = self._events()
        assert event.metadata["renotify"] is True
        assert event.metadata["previously_notified_at"] == ""
        assert wi.last_error_notified_at == event.occurred_at

    async def test_a_repeat_restarts_the_window(self, db_session):
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=25)
        )

        await self._fail(db_session, wi)
        await self._fail(db_session, wi)

        assert len(self._events()) == 1

    async def test_the_stamp_commits_before_the_dispatch(self, db_session):
        """At most once per window even when the dispatch blows up: the stamp
        is committed with the failure, so a retried or crashed apply cannot
        re-send. A dispatch-then-stamp order would storm under #269's wedge."""
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=25)
        )
        previous = wi.last_error_notified_at
        self.dispatch.side_effect = RuntimeError("notifier misconfigured")

        with pytest.raises(RuntimeError):
            await self._fail(db_session, wi)
        await db_session.rollback()
        await db_session.refresh(wi)

        # This failure's own stamp, committed: the same `now` as the check.
        assert wi.last_error_notified_at != previous
        assert wi.last_error_notified_at == wi.last_checked_at

    async def test_a_wedged_invalid_request_options_item_does_not_storm(self, db_session):
        """#269: each refusal clears validators and fails again — one repeat
        per window however often the cycle turns."""
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=25)
        )

        for _ in range(3):
            wi.etag = 'W/"v7"'
            await self._fail(
                db_session, wi, reason=INVALID_REQUEST_OPTIONS_REASON, status_code=None
            )

        assert [e.metadata["renotify"] for e in self._events()] == [True]
        assert wi.etag is None

    async def test_recovery_clears_the_stamp(self, db_session):
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=1)
        )
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.NOT_MODIFIED
        row.status_code = 304
        row.fact_at = NOW
        await db_session.flush()

        await apply_fetch_not_modified(row.command_id)

        assert wi.health_status == WatchHealthStatus.OK
        assert wi.last_error_notified_at is None
        assert [e.event_type for e in self._events()] == [WatchEventType.WATCH_RECOVERED]

    async def test_refailure_after_recovery_is_a_first_error_again(self, db_session):
        wi = await self._item(
            db_session, health=WatchHealthStatus.ERROR, notified_ago=timedelta(hours=1)
        )
        ok = await create_fetch_command(db_session, wi, now=NOW)
        ok.status = FetchCommandStatus.NOT_MODIFIED
        ok.status_code = 304
        ok.fact_at = NOW
        await db_session.flush()
        await apply_fetch_not_modified(ok.command_id)

        await self._fail(db_session, wi)

        recovered, error = self._events()
        assert recovered.event_type == WatchEventType.WATCH_RECOVERED
        assert error.event_type == WatchEventType.WATCH_ERROR
        assert error.metadata["renotify"] is False
        assert wi.last_error_notified_at == error.occurred_at


class TestForcedFetchLineage:
    """CR-1: a forced full fetch must survive a re-issue.

    Check-now promises a real re-read. Before this, a forced command that was
    re-issued re-resolved validators from the item and could send
    ``If-None-Match``, so the operator's forced check could be answered 304 and
    produce no bytes at all, with nothing saying the request had been downgraded.
    ``reissue_fetch_command`` serves the reaper and the processor's
    ``input_unreadable`` alike.
    """

    async def _reissued(self, db_session, monkeypatch, *, forced: bool) -> FetchCommand:
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi, row = await _row_with_fact(db_session)
        wi.etag = 'W/"v2"'
        # Real clock: the re-issue resolves validators against datetime.now, so
        # a fixture-era stamp would age the pair out and pass for the wrong reason.
        wi.last_full_fetch_at = datetime.now(UTC) - timedelta(hours=1)
        wi.blob_expires_at = wi.last_full_fetch_at + timedelta(days=7)
        wi.processor_version = "0.19.7+1"
        wi.validator_source_key = validator_source_key(
            effective_url=wi.effective_url, source_specs=wi.source_specs, generation="0.19.7+1"
        )
        row.forced_full_fetch = forced
        row.status = FetchCommandStatus.EXPIRED
        await db_session.flush()

        new_id = await reissue_fetch_command(db_session, wi, row, AsyncMock())
        return await db_session.get(FetchCommand, new_id)

    async def test_a_reissue_keeps_the_forced_intent(self, db_session, monkeypatch):
        reissued = await self._reissued(db_session, monkeypatch, forced=True)
        assert reissued.forced_full_fetch is True
        assert reissued.request_etag is None

    async def test_an_ordinary_reissue_still_replays(self, db_session, monkeypatch):
        reissued = await self._reissued(db_session, monkeypatch, forced=False)
        assert reissued.forced_full_fetch is False
        assert reissued.request_etag == 'W/"v2"'


class TestValidatorStorage:
    """#269 part 2: the item's replayable pair, written from the closing fact.

    Item-level, never fingerprint-level (issuer contract MUST-5), and only from
    the fact that closed the item's *latest* command — which is what the existing
    supersession guard already establishes. What a 200 stores is the derived
    leg's (``tests/workers/test_process_commands.py``); these are the paths
    that close a check without one.
    """

    async def test_not_modified_apply_keeps_the_pair_and_its_age(self, db_session, monkeypatch):
        # A 304 brings no validators and no bytes: the stored pair is still
        # current, and the age ceiling must keep running toward a full re-fetch.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.etag = 'W/"v2"'
        wi.last_modified = "Wed, 13 Aug 2026 10:00:00 GMT"
        wi.last_full_fetch_at = NOW - timedelta(hours=6)
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.NOT_MODIFIED
        row.status_code = 304
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        await apply_fetch_not_modified(row.command_id)

        assert wi.etag == 'W/"v2"'
        assert wi.last_modified == "Wed, 13 Aug 2026 10:00:00 GMT"
        assert wi.last_full_fetch_at == NOW - timedelta(hours=6)

    async def test_not_modified_apply_keeps_the_blob_horizon(self, db_session, monkeypatch):
        # A 304 brings no blob, so nothing renewed and the half-life keeps running.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.last_full_fetch_at = NOW - timedelta(hours=6)
        wi.blob_expires_at = NOW - timedelta(hours=6) + timedelta(days=7)
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.NOT_MODIFIED
        row.status_code = 304
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        await apply_fetch_not_modified(row.command_id)

        assert wi.blob_expires_at == NOW - timedelta(hours=6) + timedelta(days=7)

    async def test_invalid_request_options_clears_the_pair(self, db_session, monkeypatch):
        # The one loop hazard: the refusal happens BEFORE any request, so a bad
        # stored validator would be re-snapshotted and refused every cycle,
        # forever, each time costing ERROR health and a WATCH_ERROR.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.etag = 'W/"unsendable"'
        wi.last_modified = "Wed, 13 Aug 2026 10:00:00 GMT"
        wi.validator_source_key = "sha256:whatever"
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = "invalid_request_options"
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", AsyncMock(return_value=0))

        await apply_fetch_failure(row.command_id)

        assert wi.etag is None
        assert wi.last_modified is None
        assert wi.validator_source_key is None

    async def test_an_ordinary_failure_leaves_the_pair_alone(self, db_session, monkeypatch):
        # A 503 says nothing about our validators; forgetting them would buy a
        # full re-fetch for every transient outage.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.etag = 'W/"v2"'
        row = await create_fetch_command(db_session, wi, now=NOW)
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = "http_status"
        row.status_code = 503
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", AsyncMock(return_value=0))

        await apply_fetch_failure(row.command_id)

        assert wi.etag == 'W/"v2"'
