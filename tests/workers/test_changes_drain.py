"""End-to-end drain worker tests with fakeredis + test DB."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from fakeredis import aioredis as fakeredis_aio
from ulid import ULID

from src.core.changes.publisher import ChangePublisher
from src.core.models.change import Change
from src.workers.changes_drain import (
    DRAIN_ADVISORY_LOCK_ID,
    _build_envelope,
    drain_changes_outbox,
)
from tests.conftest import make_snapshot, make_watch

pytestmark = pytest.mark.integration


def _snapshot_kwargs(content_hash="hash1"):
    """Return standard kwargs for snapshot creation."""
    return {
        "content_hash": content_hash,
        "simhash": 123,
        "storage_path": "s/1.html",
        "text_path": "s/1.txt",
        "storage_backend": "local",
        "chunk_count": 1,
        "text_bytes": 100,
        "fetch_duration_ms": 100,
    }


@pytest.fixture
async def fake_redis():
    client = fakeredis_aio.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def drain_with_test_session(db_session, fake_redis):
    """Patch get_session_factory and ChangePublisher to use test DB + fakeredis."""

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    def _session_factory():
        return _session_cm()

    publisher_init = ChangePublisher.__init__

    def patched_publisher_init(self, *, redis_client=None):
        publisher_init(self, redis_client=fake_redis)

    with patch("src.workers.changes_drain.get_session_factory", return_value=_session_factory):
        with patch.object(ChangePublisher, "__init__", patched_publisher_init):
            yield


@pytest.mark.asyncio
async def test_drain_publishes_unpublished(
    db_session, make_change, fake_redis, drain_with_test_session
):
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    c1 = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)
    c2 = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)
    await db_session.commit()

    result = await drain_changes_outbox()
    assert result["published"] == 2
    assert result["failed"] == 0

    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 2
    payload_change_ids = {json.loads(e[1][b"payload"])["change_id"] for e in entries}
    assert payload_change_ids == {str(c1.id), str(c2.id)}


@pytest.mark.asyncio
async def test_drain_marks_rows_published(
    db_session, make_change, fake_redis, drain_with_test_session
):
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    c = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)
    await db_session.commit()

    await drain_changes_outbox()

    await db_session.refresh(c)
    assert c.published_to_bus_at is not None
    assert c.bus_message_id is not None


@pytest.mark.asyncio
async def test_drain_skips_already_published(
    db_session, make_change, fake_redis, drain_with_test_session
):
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)
    await db_session.commit()

    await drain_changes_outbox()
    result = await drain_changes_outbox()  # second call

    assert result["published"] == 0
    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_envelope_includes_info_item_and_fingerprints(db_session):
    """Envelope schema v2 carries info_item_id, info_spec_id, fingerprints."""
    watch = await make_watch(db_session)
    prev_snap = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    curr_snap = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    spec_id = ULID()
    change = Change(
        watch_id=watch.id,
        previous_snapshot_id=prev_snap.id,
        current_snapshot_id=curr_snap.id,
        info_item_id=watch.info_item_id,
        info_spec_id=spec_id,
        previous_fingerprint=12345,
        current_fingerprint=67890,
        detected_at=datetime.now(UTC),
    )
    db_session.add(change)
    await db_session.flush()

    payload = _build_envelope(change)
    body = json.loads(payload)
    assert body["schema_version"] == 2
    assert body["info_item_id"] == str(change.info_item_id)
    assert body["info_spec_id"] == str(change.info_spec_id)
    assert body["previous_fingerprint"] == 12345
    assert body["current_fingerprint"] == 67890


@pytest.mark.asyncio
async def test_drain_uses_info_item_id_as_partition_key(
    db_session, make_change, fake_redis, drain_with_test_session
):
    """Stream entry's `key` field is str(info_item_id), not watch_id."""
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    # info_item_id differs from watch_id so a key mix-up is detectable.
    await make_change(
        watch=watch,
        current_snapshot=s2,
        previous_snapshot=s1,
        info_item_id=watch.info_item_id,
        info_spec_id=ULID(),
        previous_fingerprint=1,
        current_fingerprint=2,
    )
    await db_session.commit()

    await drain_changes_outbox()

    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields[b"key"] == str(watch.info_item_id).encode("utf-8")
    assert fields[b"key"] != str(watch.id).encode("utf-8")
    # Header schema_version (wire field "hdr.schema_version") matches envelope.
    assert fields[b"hdr.schema_version"] == b"2"


@pytest.mark.asyncio
async def test_drain_skips_when_lock_held(
    db_session, make_change, fake_redis, drain_with_test_session, test_engine
):
    """If pg_try_advisory_xact_lock returns false, drain returns early.

    `pg_advisory_lock` is session-scoped; `pg_try_advisory_xact_lock` is
    transaction-scoped. Both share the same lock space, so a session-level
    holder blocks transaction-level acquirers.
    """
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    await make_change(
        watch=watch,
        current_snapshot=s2,
        previous_snapshot=s1,
        info_item_id=watch.info_item_id,
        info_spec_id=ULID(),
    )
    await db_session.commit()

    # Use a separate connection (not the savepoint-wrapped db_session) so the
    # session-level advisory lock is visible across connections.
    async with test_engine.connect() as holder_conn:
        await holder_conn.execute(
            sa.text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": DRAIN_ADVISORY_LOCK_ID},
        )
        try:
            result = await drain_changes_outbox(batch_size=10)
            assert result == {"published": 0, "failed": 0, "skipped": True}
            entries = await fake_redis.xrange("info.changes")
            assert entries == []
        finally:
            await holder_conn.execute(
                sa.text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": DRAIN_ADVISORY_LOCK_ID},
            )
