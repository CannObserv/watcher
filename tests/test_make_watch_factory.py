"""Tests for the module-level factory helpers in tests/conftest.py.

Phase 5: ``make_watch`` auto-creates an InfoSource (not InfoItem + InfoSpec).
The Watch row references ``info_source_id``; ``info_item_id`` is gone.
"""

import pytest
from sqlalchemy import select

from src.core.models.watch import ContentType, Watch
from tests._information_test_models import InfoItem, InfoSource
from tests.conftest import make_info_item, make_info_source, make_snapshot, make_watch

pytestmark = pytest.mark.integration


async def test_make_info_item_persists(db_session):
    item = await make_info_item(db_session, name="Sample")
    fetched = (await db_session.execute(select(InfoItem))).scalar_one()
    assert fetched.info_item_id == item.info_item_id
    assert fetched.name == "Sample"


async def test_make_info_source_persists(db_session):
    source = await make_info_source(db_session, url="https://example.org/page")
    fetched = (await db_session.execute(select(InfoSource))).scalar_one()
    assert fetched.info_source_id == source.info_source_id
    assert fetched.source_spec["target"]["url"] == "https://example.org/page"


async def test_make_watch_creates_info_source(db_session):
    watch = await make_watch(db_session, name="Auto Watch")
    assert isinstance(watch, Watch)
    assert watch.name == "Auto Watch"
    assert watch.content_type == ContentType.HTML
    assert watch.info_source_id is not None

    sources = (await db_session.execute(select(InfoSource))).scalars().all()
    assert len(sources) == 1
    assert sources[0].info_source_id == watch.info_source_id


async def test_make_watch_url_passed_to_info_source(db_session):
    """URL is stored in the InfoSource source_spec, not on Watch."""
    await make_watch(db_session, name="URL Watch", url="https://example.com")
    sources = (await db_session.execute(select(InfoSource))).scalars().all()
    assert len(sources) == 1
    assert sources[0].source_spec["target"]["url"] == "https://example.com"
    # Watch itself has no url column in Phase 5
    assert not hasattr(Watch, "url")


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
