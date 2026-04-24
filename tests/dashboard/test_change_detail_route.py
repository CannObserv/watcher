"""Smoke test for GET /changes/{change_id}: diff mount appears when snapshots differ."""

import pytest

from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import Watch

pytestmark = pytest.mark.integration


async def _make_change(db_session, tmp_path, prev_text: str, curr_text: str):
    """Build a Watch + two Snapshots (text on disk, absolute paths) + a Change.

    Uses absolute paths so LocalStorage.load(base_dir / path) resolves to the
    tmp_path regardless of STORAGE_BASE_DIR.
    """
    prev_path = tmp_path / "previous.txt"
    curr_path = tmp_path / "current.txt"
    prev_path.write_text(prev_text)
    curr_path.write_text(curr_text)

    watch = Watch(name="Diff Route Test", url="https://example.com", content_type="html")
    db_session.add(watch)
    await db_session.flush()

    snap_common = dict(
        watch_id=watch.id,
        simhash=0,
        chunk_count=1,
        text_bytes=len(prev_text.encode()),
        fetch_duration_ms=50,
        fetcher_used="http",
    )
    prev_snap = Snapshot(
        **snap_common,
        content_hash="a" * 64,
        storage_path=str(prev_path),
        text_path=str(prev_path),
    )
    curr_snap = Snapshot(
        **snap_common,
        content_hash="b" * 64,
        storage_path=str(curr_path),
        text_path=str(curr_path),
    )
    db_session.add_all([prev_snap, curr_snap])
    await db_session.flush()

    db_session.add(
        SnapshotChunk(
            snapshot_id=curr_snap.id,
            chunk_index=0,
            chunk_type="section",
            chunk_label="Main",
            content_hash="b" * 64,
            simhash=0,
            char_count=len(curr_text),
            excerpt=curr_text[:80],
        )
    )

    change = Change(
        watch_id=watch.id,
        previous_snapshot_id=prev_snap.id,
        current_snapshot_id=curr_snap.id,
        change_metadata={"added": [], "modified": ["Main"], "removed": []},
    )
    db_session.add(change)
    await db_session.flush()
    return change


class TestChangeDetailRoute:
    async def test_response_contains_diff_mount_when_snapshots_differ(
        self, client, db_session, tmp_path
    ):
        change = await _make_change(
            db_session, tmp_path, prev_text="hello\nworld\n", curr_text="hello\nplanet\n"
        )
        resp = await client.get(f"/changes/{change.id}")
        assert resp.status_code == 200
        assert b"data-unified-diff" in resp.content
        assert b"diff-mount" in resp.content

    async def test_response_shows_no_changes_when_snapshots_identical(
        self, client, db_session, tmp_path
    ):
        change = await _make_change(
            db_session, tmp_path, prev_text="same\ncontent\n", curr_text="same\ncontent\n"
        )
        resp = await client.get(f"/changes/{change.id}")
        assert resp.status_code == 200
        assert b"No textual differences found" in resp.content
        assert b"data-unified-diff" not in resp.content

    async def test_unknown_mode_returns_422(self, client, db_session, tmp_path):
        """Literal['extracted','raw'] rejects unknown modes with FastAPI's 422."""
        change = await _make_change(db_session, tmp_path, prev_text="a\n", curr_text="b\n")
        resp = await client.get(f"/changes/{change.id}?mode=bogus")
        assert resp.status_code == 422

    async def test_partial_diff_unknown_mode_returns_422(self, client, db_session, tmp_path):
        change = await _make_change(db_session, tmp_path, prev_text="a\n", curr_text="b\n")
        resp = await client.get(f"/partials/diff/{change.id}?mode=bogus")
        assert resp.status_code == 422
