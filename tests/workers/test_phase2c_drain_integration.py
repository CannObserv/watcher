"""Phase 2c end-to-end integration test.

Exercises check_watch -> Change row populated -> drain_changes_outbox
-> fakeredis stream entry with v2 envelope shape. The Information SDK
is mocked via the ``info_client`` fixture (DB-backed); the fetcher is
mocked inline; Redis is replaced with fakeredis.

Two checks run back-to-back so the second one diffs against the first
snapshot and produces a Change row (no Change is created on the very
first check).
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis import aioredis as fakeredis_aio
from sqlalchemy import select

import src.workers.tasks as tasks_mod
from src.core.changes.publisher import ChangePublisher
from src.core.models.change import Change
from src.core.models.watch import ContentType
from src.core.registry import ServiceRegistry
from src.core.storage import LocalStorage
from src.workers.changes_drain import drain_changes_outbox
from src.workers.tasks import check_watch
from tests.conftest import make_info_item, make_info_spec, make_watch

pytestmark = pytest.mark.integration


def _fake_fetch_result(content: bytes):
    """Build a minimal FetchResult-shaped MagicMock."""
    result = MagicMock()
    result.is_success = True
    result.status_code = 200
    result.content = content
    result.fetcher_used = "http"
    result.duration_ms = 12
    result.headers = {}
    return result


@pytest.fixture
async def fake_redis():
    client = fakeredis_aio.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def session_redirect(db_session):
    """Patch tasks_mod and changes_drain session factories to share db_session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


@pytest.mark.asyncio
async def test_check_to_drain_to_stream(
    db_session,
    monkeypatch,
    tmp_path,
    fake_redis,
):
    """check_watch produces a Change with info_item_id; drain publishes envelope v2."""
    # Seed an InfoItem + primary InfoSpec, then a Watch referencing it.
    info_item = await make_info_item(db_session, name="Phase2c E2E")
    spec = await make_info_spec(db_session, info_item, url="https://example.com/e2e")
    watch = await make_watch(
        db_session,
        name="Phase2c E2E",
        info_item_id=info_item.info_item_id,
        url="https://example.com/e2e",
        content_type=ContentType.HTML,
    )
    await db_session.commit()

    # SDK mock: returns the seeded primary spec via get_primary_info_spec.
    fake_doc = MagicMock()
    fake_doc.to_dict = MagicMock(return_value=dict(spec.document))
    fake_spec_out = MagicMock()
    fake_spec_out.info_item_id = str(info_item.info_item_id)
    fake_spec_out.info_spec_id = str(spec.info_spec_id)
    fake_spec_out.document = fake_doc
    fake_client = MagicMock()
    fake_client.get_primary_info_spec = AsyncMock(return_value=fake_spec_out)

    # Two-stage fetch results: first content baseline, second content differs
    # so the pipeline records a Change on the second check.
    fetch_results = [
        _fake_fetch_result(b"<html><body><p>Baseline content</p></body></html>"),
        _fake_fetch_result(b"<html><body><p>Updated content here</p></body></html>"),
    ]
    fake_fetcher = MagicMock()
    fake_fetcher.fetch = AsyncMock(side_effect=fetch_results)

    reg = ServiceRegistry(fetcher=fake_fetcher, information_client=fake_client)

    # Redirect tasks_mod's session factory + storage to the test DB / tmp_path.
    monkeypatch.setattr(tasks_mod, "default_storage", LocalStorage(base_dir=tmp_path))

    @asynccontextmanager
    async def _ctx():
        yield db_session

    def _factory():
        f = MagicMock()
        f.return_value = _ctx()
        return f

    monkeypatch.setattr(tasks_mod, "get_session_factory", _factory)

    # First check: baseline snapshot, no Change row yet.
    await check_watch(str(watch.id), registry=reg)
    # Second check: content differs -> Change row recorded.
    await check_watch(str(watch.id), registry=reg)
    await db_session.commit()

    # Verify Change row carries info_item_id, info_spec_id, fingerprint.
    rows = (await db_session.execute(select(Change))).scalars().all()
    assert len(rows) == 1, f"expected exactly 1 Change, got {len(rows)}"
    change = rows[0]
    assert change.info_item_id == info_item.info_item_id
    assert change.info_spec_id is not None
    assert change.current_fingerprint is not None
    assert change.published_to_bus_at is None  # not yet drained

    # Drain: redirect drain's session factory + ChangePublisher to fakeredis.
    publisher_init = ChangePublisher.__init__

    def patched_publisher_init(self, *, redis_client=None):
        publisher_init(self, redis_client=fake_redis)

    with patch(
        "src.workers.changes_drain.get_session_factory",
        return_value=_factory(),
    ):
        with patch.object(ChangePublisher, "__init__", patched_publisher_init):
            result = await drain_changes_outbox(batch_size=10)

    assert result == {"published": 1, "failed": 0}

    # Verify the published envelope on the fakeredis stream.
    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1
    fields = entries[0][1]
    # Partition key is info_item_id (Phase 2c v2 shape).
    assert fields[b"key"] == str(info_item.info_item_id).encode("utf-8")
    assert fields[b"hdr.schema_version"] == b"2"

    body = json.loads(fields[b"payload"])
    assert body["schema_version"] == 2
    assert body["info_item_id"] == str(info_item.info_item_id)
    assert body["info_spec_id"] == str(spec.info_spec_id)
    assert body["current_fingerprint"] is not None
    assert body["change_id"] == str(change.id)
    assert body["watch_id"] == str(watch.id)

    # Row marked published.
    await db_session.refresh(change)
    assert change.published_to_bus_at is not None
    assert change.bus_message_id is not None
