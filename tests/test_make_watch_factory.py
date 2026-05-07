"""Tests for the module-level factory helpers in tests/conftest.py.

Verifies the Phase 2c migration shim: ``make_watch`` auto-creates an
InfoItem + primary InfoSpec and produces a Watch row that adapts to the
current model shape via ``hasattr`` guards.
"""

import pytest
from sqlalchemy import select

from src.core.models.watch import ContentType, Watch
from tests._information_test_models import InfoItem, InfoSpec
from tests.conftest import make_info_item, make_info_spec, make_snapshot, make_watch

pytestmark = pytest.mark.integration


async def test_make_info_item_persists(db_session):
    item = await make_info_item(db_session, name="Sample")
    fetched = (await db_session.execute(select(InfoItem))).scalar_one()
    assert fetched.info_item_id == item.info_item_id
    assert fetched.name == "Sample"


async def test_make_info_spec_builds_v1_document(db_session):
    item = await make_info_item(db_session, name="Spec Owner")
    spec = await make_info_spec(db_session, item, url="https://example.org/page", selector=".main")
    fetched = (await db_session.execute(select(InfoSpec))).scalar_one()
    assert fetched.info_spec_id == spec.info_spec_id
    assert fetched.info_item_id == item.info_item_id
    assert fetched.document == {
        "schema_version": 1,
        "target": {"url": "https://example.org/page"},
        "extraction": {"algorithm": "css", "selector": ".main"},
        "fingerprint": {"algorithm": "simhash"},
    }
    assert fetched.priority == 1
    assert fetched.active is True


async def test_make_watch_creates_info_item_and_spec(db_session):
    watch = await make_watch(db_session, name="Auto Watch")
    assert isinstance(watch, Watch)
    assert watch.name == "Auto Watch"
    assert watch.content_type == ContentType.HTML

    items = (await db_session.execute(select(InfoItem))).scalars().all()
    specs = (await db_session.execute(select(InfoSpec))).scalars().all()
    assert len(items) == 1
    assert len(specs) == 1
    assert specs[0].info_item_id == items[0].info_item_id


async def test_make_watch_url_currently_present_via_hasattr_guard(db_session):
    """Task 0: Watch model still has ``url``. The shim must include it."""
    watch = await make_watch(db_session, name="URL Watch", url="https://nope.example")
    if hasattr(Watch, "url"):
        assert watch.url == "https://nope.example"


async def test_make_snapshot_attaches_to_watch(db_session):
    watch = await make_watch(db_session, name="Snap Watch")
    snap = await make_snapshot(
        db_session,
        watch,
        content_hash="abc",
        simhash=0,
        storage_path="/tmp/x",
        text_path="/tmp/x.txt",
        chunk_count=1,
        text_bytes=10,
        fetch_duration_ms=1,
    )
    assert snap.watch_id == watch.id
    assert snap.fetcher_used == "http"
