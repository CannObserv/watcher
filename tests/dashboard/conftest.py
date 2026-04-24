"""Dashboard-scoped fixtures shared across dashboard test modules."""

import pytest

from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import Watch


@pytest.fixture
async def make_change_with_snapshots(db_session, tmp_path):
    """Build a Watch + two Snapshots + optional SnapshotChunk + a Change.

    When to reach for this fixture vs. the global builders in ``tests/conftest.py``:

    - ``tests/conftest.py::make_change`` takes **pre-built** Watch/Snapshot
      objects (plus ``make_watch`` / ``make_snapshot`` fixtures) — reach for
      it when a test needs fine-grained control over individual snapshot
      fields, or wants to share one Watch across multiple Changes.
    - This fixture builds the **entire** Watch→Snapshots→(SnapshotChunk)→Change
      chain in one call — reach for it for end-to-end route tests or for
      ``get_change_detail``-style queries where the test only cares about
      the final ``Change`` row and common default shape.

    The async factory returned exposes these keyword-only knobs:

    - ``prev_text``, ``curr_text``: string content for the two snapshots.
      Defaults differ (``"prev"`` / ``"curr"``) so the default output is an
      interesting (non-empty) diff — the common case for route tests.
    - ``write_files``: when True, writes prev_text/curr_text into ``tmp_path``
      and uses absolute paths for ``text_path``/``storage_path``. Because
      ``Path(base_dir) / Path(abs_path)`` is ``abs_path`` in pathlib,
      ``LocalStorage(base_dir).load(abs_path)`` reads from ``tmp_path``
      regardless of the real ``STORAGE_BASE_DIR``. Needed by route-level
      tests that exercise ``_load_snapshot_text``. When False, uses
      placeholder paths ``/tmp/s`` / ``/tmp/t`` (DB-only tests).
    - ``include_chunk``: when True (default), seeds a single SnapshotChunk
      on the current snapshot (``chunk_count=1``). When False, no chunk is
      inserted and ``chunk_count=0`` — use for DB-only tests that don't
      need chunk rendering.
    - ``screenshot_paths``: optional ``(prev, curr)`` tuple that sets
      ``screenshot_path`` on each snapshot.
    - Any remaining kwargs are forwarded to the ``Change`` constructor
      (e.g. ``change_metadata``, ``visual_change_score``).
    """

    async def _factory(
        *,
        prev_text: str = "prev",
        curr_text: str = "curr",
        write_files: bool = False,
        watch_name: str = "W",
        watch_url: str = "https://example.com",
        include_chunk: bool = True,
        screenshot_paths: tuple[str, str] | None = None,
        **change_kwargs,
    ) -> Change:
        if write_files:
            prev_path = tmp_path / "previous.txt"
            curr_path = tmp_path / "current.txt"
            prev_path.write_text(prev_text)
            curr_path.write_text(curr_text)
            prev_storage = str(prev_path)
            curr_storage = str(curr_path)
        else:
            prev_storage = "/tmp/s"
            curr_storage = "/tmp/t"

        watch = Watch(name=watch_name, url=watch_url, content_type="html")
        db_session.add(watch)
        await db_session.flush()

        # This fixture only seeds chunks on curr_snap (when include_chunk=True);
        # prev_snap has no chunks, so prev chunk_count is always 0 to match reality.
        curr_chunk_count = 1 if include_chunk else 0
        prev_snap = Snapshot(
            watch_id=watch.id,
            content_hash="a" * 64,
            simhash=0,
            storage_path=prev_storage,
            text_path=prev_storage,
            chunk_count=0,
            text_bytes=len(prev_text.encode()),
            fetch_duration_ms=50,
            fetcher_used="http",
            screenshot_path=screenshot_paths[0] if screenshot_paths else None,
        )
        curr_snap = Snapshot(
            watch_id=watch.id,
            content_hash="b" * 64,
            simhash=0,
            storage_path=curr_storage,
            text_path=curr_storage,
            chunk_count=curr_chunk_count,
            text_bytes=len(curr_text.encode()),
            fetch_duration_ms=50,
            fetcher_used="http",
            screenshot_path=screenshot_paths[1] if screenshot_paths else None,
        )
        db_session.add_all([prev_snap, curr_snap])
        await db_session.flush()

        if include_chunk:
            db_session.add(
                SnapshotChunk(
                    snapshot_id=curr_snap.id,
                    chunk_index=0,
                    chunk_type="section",
                    chunk_label="Main",
                    content_hash="c" * 64,
                    simhash=0,
                    char_count=len(curr_text),
                    excerpt=curr_text[:80],
                )
            )

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
            **change_kwargs,
        )
        db_session.add(change)
        await db_session.flush()
        return change

    return _factory
