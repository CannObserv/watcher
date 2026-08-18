"""Tests for apply_fetch_blob / apply_fetch_failure / reap_fetch_commands (#241 step 2).

The apply path must leave the SAME bookkeeping the local fetch path leaves —
health, ``last_checked_at``, check audits, error surfacing — and must be safe
under the bus's actual delivery semantics: duplicates (status guard),
no ordering (supersession guard), and expiring blobs (re-issue, not error).
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from sqlalchemy import select

import src.workers.fetch_commands as fc_mod
from src.core.fetch_commands import create_fetch_command, get_open_command
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.fetch_command import OPEN_STATUSES, FetchCommand, FetchCommandStatus
from src.core.models.watched_item import WatchHealthStatus
from src.core.notifications.events import WatchEventType
from src.core.registry import ServiceRegistry
from src.core.validators import CONDITIONAL_GET_ENV, validator_source_key
from src.workers.fetch_commands import (
    apply_fetch_blob,
    apply_fetch_failure,
    apply_fetch_not_modified,
    reap_fetch_commands,
)
from src.workers.pipeline import ExtractionError, WatchedItemResult
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 6, 17, 30, 0, tzinfo=UTC)


def _mock_session_factory(db_session):
    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


def _stub_pipeline(monkeypatch, *, changed=True, raises=None) -> AsyncMock:
    async def _proc(session, watched_item, *, raw_content, registry=None, blob=None):
        if raises is not None:
            raise raises
        return WatchedItemResult(changed=changed)

    stub = AsyncMock(side_effect=_proc)
    monkeypatch.setattr(fc_mod, "process_watched_item", stub)
    return stub


def _wire(db_session, monkeypatch, **pipeline_kwargs) -> AsyncMock:
    monkeypatch.setattr(fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session))
    return _stub_pipeline(monkeypatch, **pipeline_kwargs)


async def _row_with_fact(db_session, tmp_path, *, content=b"<p>hi</p>", **fact_over):
    wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
    row = await create_fetch_command(db_session, wi, now=NOW)
    row.status = FetchCommandStatus.IN_FLIGHT
    row.published_at = NOW
    blob = tmp_path / "blob.bin"
    blob.write_bytes(content)
    row.blob_uri = f"file://{blob}"
    row.fact_at = NOW
    row.content_fingerprint = "ab" * 32
    for key, value in fact_over.items():
        setattr(row, key, value)
    await db_session.flush()
    return wi, row


async def _audit_events(db_session, event_type) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.event_type == event_type)
    return list((await db_session.execute(stmt)).scalars().all())


class TestApplyFetchBlob:
    async def test_applies_blob_through_the_pipeline(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(db_session, tmp_path)
        stub = _wire(db_session, monkeypatch, changed=True)

        result = await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert result["applied"] is True
        assert stub.await_count == 1
        assert stub.await_args.kwargs["raw_content"] == b"<p>hi</p>"
        assert row.status == FetchCommandStatus.SUCCEEDED
        assert row.applied_at is not None
        assert wi.health_status == WatchHealthStatus.OK
        assert wi.last_checked_at is not None
        assert len(await _audit_events(db_session, EventType.CHECK_SNAPSHOT_CREATED)) == 1

    async def test_status_guard_makes_duplicates_noops(self, db_session, monkeypatch, tmp_path):
        _, row = await _row_with_fact(db_session, tmp_path)
        stub = _wire(db_session, monkeypatch)

        first = await apply_fetch_blob(row.command_id, registry=ServiceRegistry())
        second = await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert first["applied"] is True
        assert second == {"skipped": True, "reason": "status_succeeded"}
        assert stub.await_count == 1

    async def test_out_of_order_apply_is_superseded(self, db_session, monkeypatch, tmp_path):
        # A reaper re-issue racing a recovered original: the newer command
        # applied first; the older must not flap the fingerprint A→B→A.
        wi, older = await _row_with_fact(db_session, tmp_path)
        newer = await create_fetch_command(
            db_session, wi, now=NOW + timedelta(minutes=5), intent_id=older.intent_id
        )
        newer.status = FetchCommandStatus.SUCCEEDED
        newer.applied_at = NOW + timedelta(minutes=6)
        await db_session.flush()
        stub = _wire(db_session, monkeypatch)

        result = await apply_fetch_blob(older.command_id, registry=ServiceRegistry())

        assert result == {"skipped": True, "reason": "superseded"}
        assert older.status == FetchCommandStatus.SUPERSEDED
        assert stub.await_count == 0

    async def test_unreadable_blob_reissues_the_intent(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(db_session, tmp_path)
        row.blob_uri = f"file://{tmp_path}/reaped-away.bin"
        await db_session.flush()
        stub = _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await apply_fetch_blob(
            row.command_id, registry=ServiceRegistry(), bus_client=client
        )

        assert stub.await_count == 0
        assert row.status == FetchCommandStatus.EXPIRED
        new_id = result["reissued"]
        new_row = await db_session.get(FetchCommand, new_id)
        assert new_row.intent_id == row.intent_id
        assert new_row.reissue_count == 1
        assert new_row.status == FetchCommandStatus.IN_FLIGHT  # published to the fake bus

    async def test_seeds_media_type_from_raw_header_once(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(
            db_session, tmp_path, content_type_raw="application/pdf; charset=binary"
        )
        assert wi.content_media_type is None
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())
        assert wi.content_media_type == "application/pdf; charset=binary"

        # Never clobbered on a later apply.
        wi.content_media_type = "operator/override"
        another = await create_fetch_command(db_session, wi, now=NOW + timedelta(minutes=9))
        another.status = FetchCommandStatus.IN_FLIGHT
        blob = tmp_path / "b2.bin"
        blob.write_bytes(b"x")
        another.blob_uri = f"file://{blob}"
        another.content_type_raw = "text/html"
        await db_session.flush()
        await apply_fetch_blob(another.command_id, registry=ServiceRegistry())
        assert wi.content_media_type == "operator/override"

    async def test_absent_raw_header_leaves_media_type_unset(
        self, db_session, monkeypatch, tmp_path
    ):
        """A fact with no ``content_type_raw`` must not write an empty seed (#168)."""
        wi, row = await _row_with_fact(db_session, tmp_path, content_type_raw=None)
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.content_media_type is None

    async def test_extraction_error_surfaces_like_the_retired_local_path(
        self, db_session, monkeypatch, tmp_path
    ):
        wi, row = await _row_with_fact(db_session, tmp_path)
        _wire(db_session, monkeypatch, raises=ExtractionError("bytes are not a PDF"))

        result = await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert result == {"error": "extraction_failed"}
        assert row.status == FetchCommandStatus.FAILED
        assert wi.health_status == WatchHealthStatus.ERROR
        assert wi.last_checked_at is not None
        assert len(await _audit_events(db_session, EventType.CHECK_EXTRACTION_FAILED)) == 1

    async def test_blob_provenance_is_threaded_to_the_pipeline(
        self, db_session, monkeypatch, tmp_path
    ):
        """#253: the apply path holds the FetchCommand, so it supplies provenance.

        The pipeline test proves the outbox row stores what it is given; this
        proves what it is given comes from the correlated fact.
        """
        wi, row = await _row_with_fact(db_session, tmp_path, media_type="application/pdf")
        row.blob_expires_at = NOW + timedelta(days=7)
        await db_session.flush()
        stub = _wire(db_session, monkeypatch, changed=True)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        blob = stub.await_args.kwargs["blob"]
        assert blob.command_id == row.command_id
        assert blob.blob_uri == row.blob_uri
        assert blob.source_media_type == "application/pdf"
        assert blob.blob_expires_at == NOW + timedelta(days=7)

    async def test_empty_extraction_reaches_error_health_unstubbed(
        self, db_session, monkeypatch, tmp_path
    ):
        """#258 end-to-end: the real guard drives the apply path, not a stubbed raise.

        The test above proves the *handling* with a mocked ``ExtractionError``.
        This one proves the guard actually reaches it from real bytes — the
        operator-facing claim is that rot lands as ERROR health rather than as a
        content change, and only an unstubbed pipeline can show that.
        """
        wi, row = await _row_with_fact(
            db_session, tmp_path, content=b"<html><body><p>hi</p></body></html>"
        )
        wi.source_specs = [
            {"schema_version": 1, "extraction": {"algorithm": "css", "selector": ".gone"}}
        ]
        await db_session.flush()
        monkeypatch.setattr(
            fc_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert result == {"error": "extraction_failed"}
        assert row.status == FetchCommandStatus.FAILED
        assert wi.health_status == WatchHealthStatus.ERROR
        assert len(await _audit_events(db_session, EventType.CHECK_EXTRACTION_FAILED)) == 1

    async def test_redirect_divergence_is_audited(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(
            db_session, tmp_path, final_url="https://lcb.wa.gov/moved-here"
        )
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        events = await _audit_events(db_session, EventType.CHECK_REDIRECT_OBSERVED)
        assert len(events) == 1
        assert events[0].payload["final_url"] == "https://lcb.wa.gov/moved-here"


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
        stub = _stub_pipeline(monkeypatch)
        dispatch = self._spy_dispatch(monkeypatch)

        result = await apply_fetch_not_modified(row.command_id)

        assert result == {"applied": True, "not_modified": True}
        assert wi.health_status == WatchHealthStatus.OK
        assert wi.last_checked_at is not None
        # A 304 IS an observation: the origin asserted its bytes are current.
        assert wi.last_observed_at is not None
        assert row.applied_at is not None
        assert row.status == FetchCommandStatus.NOT_MODIFIED
        # No bytes → the revision-producing half never runs.
        assert stub.await_count == 0
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


class TestProbingResolution:
    """#241 step 3: a PROBING item's first fact is its probe."""

    async def test_final_url_resolves_probing_item(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(
            db_session, tmp_path, final_url="https://www.lcb.wa.gov/notices"
        )
        wi.health_status = WatchHealthStatus.PROBING
        await db_session.flush()
        _wire(db_session, monkeypatch)

        result = await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert result["applied"] is True
        assert wi.effective_url == "https://www.lcb.wa.gov/notices"
        assert wi.domain_name == "www.lcb.wa.gov"
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "www.lcb.wa.gov"))
        ).scalar_one_or_none()
        assert domain is not None  # ensure_domain upserted the new host
        assert wi.health_status == WatchHealthStatus.OK

    async def test_probing_without_redirect_just_clears_to_ok(
        self, db_session, monkeypatch, tmp_path
    ):
        wi, row = await _row_with_fact(db_session, tmp_path, final_url=None)
        original_url = wi.effective_url
        original_domain = wi.domain_name
        wi.health_status = WatchHealthStatus.PROBING
        await db_session.flush()
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.effective_url == original_url
        assert wi.domain_name == original_domain
        assert wi.health_status == WatchHealthStatus.OK

    async def test_steady_state_redirect_does_not_move_the_item(
        self, db_session, monkeypatch, tmp_path
    ):
        # Non-PROBING items keep the audit-only behaviour: Archiver stays
        # authoritative for effective_url after the probe phase.
        wi, row = await _row_with_fact(
            db_session, tmp_path, final_url="https://elsewhere.example/moved"
        )
        original_url = wi.effective_url
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.effective_url == original_url
        assert len(await _audit_events(db_session, EventType.CHECK_REDIRECT_OBSERVED)) == 1


class TestObservationFreshness:
    """#264: ``last_observed_at`` is provenance — "content was verified
    current" — distinct from ``last_checked_at``, the anti-thrash stamp that
    advances on every outcome (#168)."""

    async def test_success_advances_last_observed_at(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(db_session, tmp_path)
        _wire(db_session, monkeypatch, changed=True)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.last_observed_at is not None

    async def test_unchanged_content_still_counts_as_observed(
        self, db_session, monkeypatch, tmp_path
    ):
        # Verified-same is an observation; only *never looked* is not.
        wi, row = await _row_with_fact(db_session, tmp_path)
        _wire(db_session, monkeypatch, changed=False)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.last_observed_at is not None

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

    async def test_extraction_failure_is_not_an_observation(
        self, db_session, monkeypatch, tmp_path
    ):
        # #258: empty extraction is a failure — selector rot must not stamp
        # the content as verified current.
        wi, row = await _row_with_fact(db_session, tmp_path)
        _wire(db_session, monkeypatch, raises=ExtractionError("all specs empty"))

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.health_status == WatchHealthStatus.ERROR
        assert wi.last_observed_at is None


class TestStatusRepublishOnTransition:
    """#264: level-not-edge publishing — a health transition defers a
    watch-status republish; a steady state never does (the periodic tick
    carries it), keeping the stream off the activity-rate cost curve."""

    def _spy(self, monkeypatch) -> AsyncMock:
        spy = AsyncMock()
        monkeypatch.setattr(fc_mod, "defer_status_republish", spy)
        return spy

    async def test_first_success_transitions_and_defers(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(db_session, tmp_path)
        _wire(db_session, monkeypatch)
        spy = self._spy(monkeypatch)
        assert wi.health_status == WatchHealthStatus.UNKNOWN

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.health_status == WatchHealthStatus.OK
        assert spy.await_count == 1

    async def test_steady_ok_does_not_defer(self, db_session, monkeypatch, tmp_path):
        wi, row = await _row_with_fact(db_session, tmp_path)
        _wire(db_session, monkeypatch)
        spy = self._spy(monkeypatch)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())
        row2 = await create_fetch_command(db_session, wi, now=NOW)
        row2.status = FetchCommandStatus.IN_FLIGHT
        row2.blob_uri = row.blob_uri
        row2.fact_at = NOW
        row2.content_fingerprint = "cd" * 32
        await db_session.flush()

        await apply_fetch_blob(row2.command_id, registry=ServiceRegistry())

        assert spy.await_count == 1  # only the UNKNOWN -> OK transition

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


class TestForcedFetchLineage:
    """CR-1: a forced full fetch must survive the reaper's re-issue.

    Check-now promises a real re-read. Before this, a forced command that
    stalled past the timeout was re-issued by ``_reissue`` — which re-resolved
    validators from the item and could send ``If-None-Match``, so the operator's
    forced check could be answered 304 and produce no bytes at all, with nothing
    saying the request had been downgraded.
    """

    async def test_reissue_after_an_unreadable_blob_keeps_the_forced_intent(
        self, db_session, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi, row = await _row_with_fact(db_session, tmp_path)
        wi.etag = 'W/"v2"'
        # Real clock: _reissue resolves validators against datetime.now, so a
        # fixture-era stamp would age the pair out and pass for the wrong reason.
        wi.last_full_fetch_at = datetime.now(UTC) - timedelta(hours=1)
        wi.validator_source_key = validator_source_key(
            effective_url=wi.effective_url, source_specs=wi.source_specs
        )
        row.forced_full_fetch = True
        row.blob_uri = "file:///nonexistent/blob.bin"
        await db_session.flush()
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, bus_client=AsyncMock())

        reissued = (
            await db_session.execute(
                select(FetchCommand).where(
                    FetchCommand.watched_item_id == wi.id,
                    FetchCommand.command_id != row.command_id,
                )
            )
        ).scalar_one()
        assert reissued.forced_full_fetch is True
        assert reissued.request_etag is None

    async def test_an_ordinary_reissue_still_replays(self, db_session, monkeypatch, tmp_path):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi, row = await _row_with_fact(db_session, tmp_path)
        wi.etag = 'W/"v2"'
        wi.last_full_fetch_at = datetime.now(UTC) - timedelta(hours=1)
        wi.validator_source_key = validator_source_key(
            effective_url=wi.effective_url, source_specs=wi.source_specs
        )
        row.blob_uri = "file:///nonexistent/blob.bin"
        await db_session.flush()
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, bus_client=AsyncMock())

        reissued = (
            await db_session.execute(
                select(FetchCommand).where(
                    FetchCommand.watched_item_id == wi.id,
                    FetchCommand.command_id != row.command_id,
                )
            )
        ).scalar_one()
        assert reissued.forced_full_fetch is False
        assert reissued.request_etag == 'W/"v2"'


class TestValidatorStorage:
    """#269 part 2: the item's replayable pair, written from the closing fact.

    Item-level, never fingerprint-level (issuer contract MUST-5), and only from
    the fact that closed the item's *latest* command — which is what the existing
    supersession guard already establishes.
    """

    async def test_blob_apply_stores_the_pair_with_its_provenance(
        self, db_session, tmp_path, monkeypatch
    ):
        wi, row = await _row_with_fact(
            db_session,
            tmp_path,
            etag='W/"v2"',
            last_modified="Wed, 13 Aug 2026 10:00:00 GMT",
        )
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id)

        assert wi.etag == 'W/"v2"'
        assert wi.last_modified == "Wed, 13 Aug 2026 10:00:00 GMT"
        assert wi.last_full_fetch_at is not None
        assert wi.validator_source_key == validator_source_key(
            effective_url=wi.effective_url, source_specs=wi.source_specs
        )

    async def test_a_200_without_validators_clears_the_stored_pair(
        self, db_session, tmp_path, monkeypatch
    ):
        # Always an overwrite: the pair must describe the latest 200, so an
        # origin that stopped sending one must not leave the old one replayable.
        wi, row = await _row_with_fact(db_session, tmp_path)
        wi.etag = 'W/"stale"'
        wi.last_modified = "Mon, 11 Aug 2026 10:00:00 GMT"
        await db_session.flush()
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id)

        assert wi.etag is None
        assert wi.last_modified is None
        assert wi.last_full_fetch_at is not None

    async def test_a_superseded_apply_leaves_the_pair_alone(
        self, db_session, tmp_path, monkeypatch
    ):
        # MUST-5's failure mode in its ordering form: a late older fact must not
        # overwrite validators a newer command already stored.
        wi, row = await _row_with_fact(db_session, tmp_path, etag='W/"old"')
        newer = await create_fetch_command(db_session, wi, now=NOW + timedelta(minutes=5))
        newer.status = FetchCommandStatus.SUCCEEDED
        newer.applied_at = NOW + timedelta(minutes=5)
        wi.etag = 'W/"new"'
        await db_session.flush()
        _wire(db_session, monkeypatch)

        result = await apply_fetch_blob(row.command_id)

        assert result == {"skipped": True, "reason": "superseded"}
        assert wi.etag == 'W/"new"'

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

    async def test_extraction_failure_clears_the_pair(self, db_session, tmp_path, monkeypatch):
        """Bytes arrived but could not be extracted — do not let a 304 mask it.

        Keeping the pair would let the next cycle answer 304, and a 304 apply
        records a *successful* check: OK health on an item whose extraction is
        broken. Clearing forces the next command to fetch in full, so the
        failure keeps re-asserting itself until someone fixes the spec.
        """
        wi, row = await _row_with_fact(db_session, tmp_path, etag='W/"v2"')
        wi.etag = 'W/"v1"'
        wi.validator_source_key = validator_source_key(
            effective_url=wi.effective_url, source_specs=wi.source_specs
        )
        await db_session.flush()
        _wire(db_session, monkeypatch, raises=ExtractionError("no chunks"))
        monkeypatch.setattr(fc_mod, "dispatch_event_notifications", AsyncMock(return_value=0))

        result = await apply_fetch_blob(row.command_id)

        assert result == {"error": "extraction_failed"}
        assert wi.etag is None
        assert wi.validator_source_key is None
        # CR-2: bytes DID arrive — the stamp records the fetch, not the outcome.
        assert wi.last_full_fetch_at is not None

    async def test_probe_resolution_keys_the_pair_to_the_final_url(
        self, db_session, tmp_path, monkeypatch
    ):
        """CR-4: the same apply moves effective_url and stores the validators.

        The pair belongs to where the bytes came from, so the key must be over
        the *resolved* URL. Storing it before the probe block would key it to the
        pre-redirect URL, and every later command would silently refuse to
        replay — benign, invisible, and exactly the kind of regression a test
        has to hold still.
        """
        wi, row = await _row_with_fact(
            db_session,
            tmp_path,
            etag='W/"v2"',
            final_url="https://www.lcb.wa.gov/notices",
        )
        wi.health_status = WatchHealthStatus.PROBING
        await db_session.flush()
        _wire(db_session, monkeypatch)

        await apply_fetch_blob(row.command_id, registry=ServiceRegistry())

        assert wi.effective_url == "https://www.lcb.wa.gov/notices"
        assert wi.etag == 'W/"v2"'
        assert wi.validator_source_key == validator_source_key(
            effective_url="https://www.lcb.wa.gov/notices", source_specs=wi.source_specs
        )

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
