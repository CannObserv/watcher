"""Tests for the scratch-cache sweeper periodic task (#185 Phase A step 6)."""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.pending_archiver_sync import PendingArchiverSync

pytestmark = pytest.mark.integration

FP = "sha256:" + "a" * 64


def _async_session_factory_returning(db_session: AsyncSession):
    """Return a fake session-factory that yields the given test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


async def _make_watched_item_and_change_revision(db_session, revision_ulid_str: str):
    """Create the minimal rows needed for a PendingArchiverSync row."""
    from src.core.models.change_revision import ChangeRevision
    from src.core.models.watched_item import WatchedItem

    wi = WatchedItem(name="Sweeper Test WI")
    db_session.add(wi)
    await db_session.flush()

    rev = ChangeRevision(
        id=ULID.from_str(revision_ulid_str),
        watched_item_id=wi.id,
        content_fingerprint=FP,
        captured_at=datetime.now(UTC),
        schema_version=1,
    )
    db_session.add(rev)
    await db_session.flush()
    return wi, rev


@pytest.mark.asyncio
async def test_sweeper_deletes_files_older_than_ttl(tmp_path, monkeypatch, db_session):
    """Files older than TTL are deleted; younger files remain."""
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock()
    monkeypatch.setattr(
        mod,
        "get_registry",
        lambda: MagicMock(get_archiver_client=lambda: fake_client),
    )
    monkeypatch.setattr(
        mod,
        "get_session_factory",
        lambda: _async_session_factory_returning(db_session),
    )

    old_ulid = "01JZZZZZZZZZZZZZZZZZZZZ000"
    young_ulid = "01JZZZZZZZZZZZZZZZZZZZZ001"
    old = tmp_path / f"{old_ulid}.bin"
    young = tmp_path / f"{young_ulid}.bin"
    old.write_bytes(b"old")
    young.write_bytes(b"new")
    old_mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
    os.utime(old, (old_mtime, old_mtime))
    # young file has current mtime — within TTL

    result = await sweep_scratch_cache()
    assert result["deleted"] == 1
    assert result["skipped"] == 0
    assert not old.exists()
    assert young.exists()
    fake_client.patch_source_revision_cache.assert_awaited_once_with(
        old_ulid,
        content_cache_uri=None,
        content_cache_expires_at=None,
    )


@pytest.mark.asyncio
async def test_sweeper_skips_files_in_pending_archiver_sync(tmp_path, monkeypatch, db_session):
    """Files whose ULID is a change_revision_id in pending_archiver_sync are skipped."""
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock()
    monkeypatch.setattr(
        mod,
        "get_registry",
        lambda: MagicMock(get_archiver_client=lambda: fake_client),
    )
    monkeypatch.setattr(
        mod,
        "get_session_factory",
        lambda: _async_session_factory_returning(db_session),
    )

    reserved_str = "01JZZZZZZZZZZZZZZZZZZZZ002"
    f = tmp_path / f"{reserved_str}.bin"
    f.write_bytes(b"reserved")
    mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
    os.utime(f, (mtime, mtime))

    now = datetime.now(UTC)
    wi, rev = await _make_watched_item_and_change_revision(db_session, reserved_str)
    row = PendingArchiverSync(
        change_revision_id=rev.id,
        watched_item_id=wi.id,
        content_cache_uri=f"file://{f}",
        content_cache_expires_at=now + timedelta(seconds=600),
        next_attempt_at=now,
    )
    db_session.add(row)
    await db_session.commit()

    result = await sweep_scratch_cache()
    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert f.exists()
    fake_client.patch_source_revision_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweeper_returns_zeros_when_cache_dir_absent(tmp_path, monkeypatch, db_session):
    """Non-existent cache dir → no-op with zero counts."""
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    absent = tmp_path / "nonexistent"
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(absent))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock()
    monkeypatch.setattr(
        mod,
        "get_registry",
        lambda: MagicMock(get_archiver_client=lambda: fake_client),
    )
    monkeypatch.setattr(
        mod,
        "get_session_factory",
        lambda: _async_session_factory_returning(db_session),
    )

    result = await sweep_scratch_cache()
    assert result == {"deleted": 0, "skipped": 0, "patch_failures": 0}


@pytest.mark.asyncio
async def test_sweeper_patch_failure_is_best_effort(tmp_path, monkeypatch, db_session):
    """Archiver PATCH failure increments patch_failures but still deletes the file."""
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock(side_effect=Exception("timeout"))
    monkeypatch.setattr(
        mod,
        "get_registry",
        lambda: MagicMock(get_archiver_client=lambda: fake_client),
    )
    monkeypatch.setattr(
        mod,
        "get_session_factory",
        lambda: _async_session_factory_returning(db_session),
    )

    old_ulid = "01JZZZZZZZZZZZZZZZZZZZZ004"
    old = tmp_path / f"{old_ulid}.bin"
    old.write_bytes(b"stale")
    mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
    os.utime(old, (mtime, mtime))

    result = await sweep_scratch_cache()
    assert result["deleted"] == 1
    assert result["patch_failures"] == 1
    assert not old.exists()
