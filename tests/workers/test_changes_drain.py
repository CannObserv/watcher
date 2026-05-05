"""End-to-end drain worker tests with fakeredis + test DB."""

import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fakeredis import aioredis as fakeredis_aio

from src.core.changes.publisher import ChangePublisher
from src.workers.changes_drain import drain_changes_outbox
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
