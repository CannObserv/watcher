"""Tests for the scratch-cache sweeper periodic task (#185 Phase A step 6).

Cache-clear PATCH invariant (#194): only revisions Archiver actually received
get a PATCH, keyed on ``ChangeRevision.archiver_revision_id`` (the ID Archiver
assigned), never the scratch filename. Orphaned / un-synced scratch files are
deleted locally with no Archiver call.
"""

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


async def _make_change_revision(
    db_session,
    revision_ulid_str: str,
    *,
    archiver_revision_id: str | None = None,
):
    """Create a WatchedItem + ChangeRevision pair; return (watched_item, revision)."""
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
        archiver_revision_id=(
            ULID.from_str(archiver_revision_id) if archiver_revision_id else None
        ),
    )
    db_session.add(rev)
    await db_session.flush()
    return wi, rev


def _patch_workers(monkeypatch, mod, db_session, fake_client):
    """Wire the sweeper module's registry + session factory to test doubles."""
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


@pytest.mark.asyncio
async def test_sweeper_deletes_orphan_without_patch(tmp_path, monkeypatch, db_session):
    """Orphaned scratch (no ChangeRevision row) older than TTL: deleted, no PATCH.

    Younger files are left in place regardless.
    """
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock()
    _patch_workers(monkeypatch, mod, db_session, fake_client)

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
    fake_client.patch_source_revision_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweeper_deletes_unsynced_revision_without_patch(tmp_path, monkeypatch, db_session):
    """ChangeRevision exists but archiver_revision_id IS NULL: deleted, no PATCH."""
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock()
    _patch_workers(monkeypatch, mod, db_session, fake_client)

    rev_ulid = "01JZZZZZZZZZZZZZZZZZZZZ003"
    f = tmp_path / f"{rev_ulid}.bin"
    f.write_bytes(b"unsynced")
    mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
    os.utime(f, (mtime, mtime))

    await _make_change_revision(db_session, rev_ulid, archiver_revision_id=None)
    await db_session.commit()

    result = await sweep_scratch_cache()
    assert result["deleted"] == 1
    assert result["patch_failures"] == 0
    assert not f.exists()
    fake_client.patch_source_revision_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweeper_patches_synced_revision_on_archiver_revision_id(
    tmp_path, monkeypatch, db_session
):
    """Synced revision: PATCH keyed on archiver_revision_id, not the scratch filename."""
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock()
    _patch_workers(monkeypatch, mod, db_session, fake_client)

    rev_ulid = "01JZZZZZZZZZZZZZZZZZZZZ005"
    archiver_ulid = "01JZZZZZZZZZZZZZZZZZZZZ006"  # distinct from the filename
    f = tmp_path / f"{rev_ulid}.bin"
    f.write_bytes(b"synced")
    mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
    os.utime(f, (mtime, mtime))

    await _make_change_revision(db_session, rev_ulid, archiver_revision_id=archiver_ulid)
    await db_session.commit()

    result = await sweep_scratch_cache()
    assert result["deleted"] == 1
    assert result["patch_failures"] == 0
    assert not f.exists()
    fake_client.patch_source_revision_cache.assert_awaited_once_with(
        archiver_ulid,
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
    _patch_workers(monkeypatch, mod, db_session, fake_client)

    reserved_str = "01JZZZZZZZZZZZZZZZZZZZZ002"
    f = tmp_path / f"{reserved_str}.bin"
    f.write_bytes(b"reserved")
    mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
    os.utime(f, (mtime, mtime))

    now = datetime.now(UTC)
    wi, rev = await _make_change_revision(db_session, reserved_str)
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
    _patch_workers(monkeypatch, mod, db_session, fake_client)

    result = await sweep_scratch_cache()
    assert result == {"deleted": 0, "skipped": 0, "patch_failures": 0}


@pytest.mark.asyncio
async def test_sweeper_patch_failure_is_best_effort(tmp_path, monkeypatch, db_session):
    """Archiver PATCH failure on a synced revision increments patch_failures but still deletes."""
    from src.workers import cache_sweeper as mod
    from src.workers.cache_sweeper import sweep_scratch_cache

    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")

    fake_client = MagicMock()
    fake_client.patch_source_revision_cache = AsyncMock(side_effect=Exception("timeout"))
    _patch_workers(monkeypatch, mod, db_session, fake_client)

    rev_ulid = "01JZZZZZZZZZZZZZZZZZZZZ004"
    archiver_ulid = "01JZZZZZZZZZZZZZZZZZZZZ007"
    old = tmp_path / f"{rev_ulid}.bin"
    old.write_bytes(b"stale")
    mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
    os.utime(old, (mtime, mtime))

    await _make_change_revision(db_session, rev_ulid, archiver_revision_id=archiver_ulid)
    await db_session.commit()

    result = await sweep_scratch_cache()
    assert result["deleted"] == 1
    assert result["patch_failures"] == 1
    assert not old.exists()
