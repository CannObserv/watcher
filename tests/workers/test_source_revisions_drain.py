"""Drain worker for pending_archiver_sync — the ``content.revisions`` producer (#253).

The outbox stays; only its transport moved. What these tests pin is the split the
issue asked for: a payload that cannot be built is *deterministic* and dead-letters
at once, while a publish that fails against the broker is *transient* and retries
forever. Getting that backwards either loses revisions or wedges the drain.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.workers.source_revisions_drain import drain_pending_archiver_sync
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

FP = "sha256:" + "a" * 64
ARCHIVER_SOURCE_ID = "01HZZ00000000000000000000S"
COMMAND_ID = "01KZMNQR9B5CQZ1CRGR1E393R6"


def _async_session_factory_returning(db_session: AsyncSession):
    """Return a fake session-factory that yields the given test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


async def _setup_pending_row(db_session: AsyncSession, **over) -> tuple:
    """Create WatchedItem + ChangeRevision + a fully-provisioned outbox row."""
    now = datetime.now(UTC)
    wi = await make_watched_item(
        db_session, name="DrainTest", archiver_info_source_id=ARCHIVER_SOURCE_ID
    )
    await db_session.flush()

    rev = ChangeRevision(
        watched_item_id=wi.id,
        content_fingerprint=over.pop("content_fingerprint", FP),
        captured_at=now,
        content_size_bytes=over.pop("content_size_bytes", 1024),
        schema_version=1,
    )
    db_session.add(rev)
    await db_session.flush()

    fields = {
        "command_id": COMMAND_ID,
        "blob_uri": "file:///var/lib/replicator/blobs/abc.bin",
        "blob_expires_at": now + timedelta(days=7),
        "source_media_type": "text/html",
        "content_media_type": "text/plain; charset=utf-8",
        "spec_fingerprint": "spec1:sha256:" + "b" * 64,
        **over,
    }
    pending = PendingArchiverSync(
        change_revision_id=rev.id,
        watched_item_id=wi.id,
        next_attempt_at=now,
        **fields,
    )
    db_session.add(pending)
    await db_session.commit()

    return wi, rev, pending


def _wire(db_session, monkeypatch):
    from src.workers import source_revisions_drain as mod

    monkeypatch.setattr(
        mod, "get_session_factory", lambda: _async_session_factory_returning(db_session)
    )


class TestPublish:
    async def test_publishes_the_observation_and_removes_the_row(self, db_session, monkeypatch):
        _, rev, pending = await _setup_pending_row(db_session)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        assert result["published"] == 1
        assert await client.xlen(streams.CONTENT_REVISIONS) == 1

        remaining = (
            await db_session.execute(
                select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
            )
        ).scalar_one_or_none()
        assert remaining is None

    async def test_payload_carries_the_observation_verbatim(self, db_session, monkeypatch):
        """Every field the wire requires, sourced from the row rather than derived."""
        _, rev, pending = await _setup_pending_row(db_session)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        entries = await client.xrange(streams.CONTENT_REVISIONS)
        payload = from_wire(
            {k.decode(): v.decode() for k, v in entries[0][1].items()},
            topic=streams.CONTENT_REVISIONS,
            message_id=entries[0][0].decode(),
        ).payload

        assert payload.info_source_id == ARCHIVER_SOURCE_ID
        # Verbatim, prefix included: Archiver enforces ^sha256:[0-9a-f]{64}$ and
        # treats a violation as poison, so any reshaping here is silent data loss.
        assert payload.extracted_fingerprint == FP
        assert payload.captured_at == rev.captured_at
        assert payload.content_size_bytes == 1024
        assert payload.content_media_type == "text/plain; charset=utf-8"
        assert payload.source_media_type == "text/html"
        assert payload.blob_uri == pending.blob_uri
        assert payload.blob_expires_at == pending.blob_expires_at
        assert payload.command_id == COMMAND_ID
        assert payload.spec_fingerprint == pending.spec_fingerprint

    async def test_no_source_revision_id_is_sent(self, db_session, monkeypatch):
        """Archiver allocates: a service that does not own the registry mints no ids."""
        await _setup_pending_row(db_session)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        entries = await client.xrange(streams.CONTENT_REVISIONS)
        fields = {k.decode(): v.decode() for k, v in entries[0][1].items()}
        assert "source_revision_id" not in fields

    async def test_spec_fingerprint_absence_still_publishes(self, db_session, monkeypatch):
        """None is a legal value — a missing diagnostic must not cost a revision."""
        await _setup_pending_row(db_session, spec_fingerprint=None)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        assert result["published"] == 1


class TestClassification:
    """Transient vs deterministic — the split #253 asked to be ported."""

    async def test_unbuildable_payload_dead_letters_immediately(self, db_session, monkeypatch):
        """source_media_type is required on the wire; absence can never succeed.

        Retrying it would spin the row forever against an outcome that is
        identical every loop.
        """
        _, _, pending = await _setup_pending_row(db_session, source_media_type=None)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        assert result["dead_lettered"] == 1
        assert result["published"] == 0
        assert await client.xlen(streams.CONTENT_REVISIONS) == 0

        row = (
            await db_session.execute(
                select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
            )
        ).scalar_one()
        assert row.dead_lettered_at is not None
        assert row.last_error

    async def test_dead_lettered_rows_are_not_selected_again(self, db_session, monkeypatch):
        await _setup_pending_row(db_session, source_media_type=None)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        await drain_pending_archiver_sync(batch_size=10, bus_client=client)
        second = await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        assert second == {"published": 0, "failed": 0, "dead_lettered": 0}

    async def test_broker_failure_retries_and_never_dead_letters(self, db_session, monkeypatch):
        """A Redis outage is transient: keep the row, no data-loss cliff."""
        _, _, pending = await _setup_pending_row(db_session)
        _wire(db_session, monkeypatch)

        class _Broken:
            async def xadd(self, *a, **kw):
                raise RedisConnectionError("connection refused")

        result = await drain_pending_archiver_sync(batch_size=10, bus_client=_Broken())

        assert result["failed"] == 1
        assert result["dead_lettered"] == 0

        row = (
            await db_session.execute(
                select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
            )
        ).scalar_one()
        assert row.dead_lettered_at is None
        assert row.attempts == 1
        assert row.last_error

    async def test_transient_failure_is_exempt_from_the_attempt_ceiling(
        self, db_session, monkeypatch
    ):
        """Past the ceiling a transient error still retries — the outage may be long."""
        _, _, pending = await _setup_pending_row(db_session)
        pending.attempts = 100_001
        pending.next_attempt_at = datetime.now(UTC)
        await db_session.commit()
        _wire(db_session, monkeypatch)

        class _Broken:
            async def xadd(self, *a, **kw):
                raise RedisConnectionError("connection refused")

        result = await drain_pending_archiver_sync(batch_size=10, bus_client=_Broken())

        assert result["dead_lettered"] == 0
        row = (
            await db_session.execute(
                select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
            )
        ).scalar_one()
        assert row.dead_lettered_at is None

    async def test_one_bad_row_does_not_stop_the_batch(self, db_session, monkeypatch):
        await _setup_pending_row(db_session, source_media_type=None)
        await _setup_pending_row(db_session)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        assert result["published"] == 1
        assert result["dead_lettered"] == 1


class TestGuards:
    async def test_no_bus_configured_skips_loudly_and_keeps_rows(self, db_session, monkeypatch):
        _, _, pending = await _setup_pending_row(db_session)
        _wire(db_session, monkeypatch)

        result = await drain_pending_archiver_sync(batch_size=10, bus_client=None)

        assert result == {"skipped": "no_bus"}
        row = (
            await db_session.execute(
                select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
            )
        ).scalar_one()
        assert row.attempts == 0

    async def test_missing_change_revision_drops_the_row(self, db_session, monkeypatch):
        _, rev, pending = await _setup_pending_row(db_session)
        await db_session.delete(rev)
        await db_session.commit()
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        await drain_pending_archiver_sync(batch_size=10, bus_client=client)

        remaining = (
            await db_session.execute(
                select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
            )
        ).scalar_one_or_none()
        assert remaining is None
