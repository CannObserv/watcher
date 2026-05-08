"""Fast-tick async drain loop tests (#144 — sub-minute drain cadence).

The fast loop is in addition to the existing 1-minute periodic; it ticks
every ``CHANGES_DRAIN_INTERVAL_SECONDS`` seconds and is gated by the same
``pg_try_advisory_xact_lock(DRAIN_ADVISORY_LOCK_ID)`` guard so concurrent
drains can't double-publish.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from fakeredis import aioredis as fakeredis_aio
from ulid import ULID

from src.core.changes.publisher import ChangePublisher
from src.workers.changes_drain import (
    DEFAULT_DRAIN_INTERVAL_SECONDS,
    DRAIN_ADVISORY_LOCK_ID,
    _drain_changes_once,
    _resolve_drain_interval,
    start_changes_drain_loop,
)
from tests.conftest import make_snapshot, make_watch


def _snapshot_kwargs(content_hash="hash1"):
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
    """Patch session factory + ChangePublisher to use test DB + fakeredis."""

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


# ---------------------------------------------------------------------------
# Pure unit-level configuration tests (no DB required).
# ---------------------------------------------------------------------------


class TestResolveDrainInterval:
    """``CHANGES_DRAIN_INTERVAL_SECONDS`` env var honoured; default 10."""

    def test_default_is_ten_seconds(self, monkeypatch):
        monkeypatch.delenv("CHANGES_DRAIN_INTERVAL_SECONDS", raising=False)
        assert _resolve_drain_interval() == 10
        assert DEFAULT_DRAIN_INTERVAL_SECONDS == 10

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("CHANGES_DRAIN_INTERVAL_SECONDS", "5")
        assert _resolve_drain_interval() == 5

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHANGES_DRAIN_INTERVAL_SECONDS", "not-a-number")
        assert _resolve_drain_interval() == DEFAULT_DRAIN_INTERVAL_SECONDS

    def test_zero_or_negative_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHANGES_DRAIN_INTERVAL_SECONDS", "0")
        assert _resolve_drain_interval() == DEFAULT_DRAIN_INTERVAL_SECONDS
        monkeypatch.setenv("CHANGES_DRAIN_INTERVAL_SECONDS", "-3")
        assert _resolve_drain_interval() == DEFAULT_DRAIN_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Fast-tick loop behaviour against the test DB.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fast_tick_drains_pending_rows(
    db_session, make_change, fake_redis, drain_with_test_session
):
    """A single tick (via ``_drain_changes_once``) publishes pending rows."""
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
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

    result = await _drain_changes_once()

    assert result == {"published": 1, "failed": 0, "skipped_due_to_lock": False}
    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fast_tick_skips_when_lock_held(
    db_session, make_change, fake_redis, drain_with_test_session, test_engine
):
    """When another holder owns ``DRAIN_ADVISORY_LOCK_ID``, the tick reports skipped."""
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

    async with test_engine.connect() as holder_conn:
        await holder_conn.execute(
            sa.text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": DRAIN_ADVISORY_LOCK_ID},
        )
        try:
            result = await _drain_changes_once()
        finally:
            await holder_conn.execute(
                sa.text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": DRAIN_ADVISORY_LOCK_ID},
            )

    assert result == {"published": 0, "failed": 0, "skipped_due_to_lock": True}
    entries = await fake_redis.xrange("info.changes")
    assert entries == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_back_to_back_ticks_do_not_double_publish(
    db_session, make_change, fake_redis, drain_with_test_session
):
    """Two ticks in quick succession publish each change exactly once."""
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash1"))
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    c = await make_change(
        watch=watch,
        current_snapshot=s2,
        previous_snapshot=s1,
        info_item_id=watch.info_item_id,
        info_spec_id=ULID(),
    )
    await db_session.commit()

    first = await _drain_changes_once()
    second = await _drain_changes_once()

    assert first["published"] == 1
    assert second["published"] == 0
    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1
    body = json.loads(entries[0][1][b"payload"])
    assert body["change_id"] == str(c.id)


# ---------------------------------------------------------------------------
# Loop lifecycle (no DB, fully mocked drain coroutine).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_changes_drain_loop_returns_task_and_ticks(monkeypatch):
    """``start_changes_drain_loop`` returns an ``asyncio.Task`` that calls drain."""
    calls: list[int] = []

    async def fake_drain(*, batch_size=100, publisher=None):
        calls.append(1)
        return {"published": 0, "failed": 0, "skipped_due_to_lock": False}

    monkeypatch.setattr("src.workers.changes_drain._drain_changes_once", fake_drain)

    task = await start_changes_drain_loop(interval=0.01)
    try:
        # Allow a few ticks to fire.
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(calls) >= 1, "fast loop must invoke drain at least once"


@pytest.mark.asyncio
async def test_drain_loop_respects_cancel(monkeypatch):
    """Cancelling the loop completes promptly without re-entering drain."""
    drain_started = asyncio.Event()
    drain_completed = asyncio.Event()

    async def fake_drain(*, batch_size=100, publisher=None):
        drain_started.set()
        # Simulate non-trivial work that must finish before shutdown returns.
        await asyncio.sleep(0.02)
        drain_completed.set()
        return {"published": 0, "failed": 0, "skipped_due_to_lock": False}

    monkeypatch.setattr("src.workers.changes_drain._drain_changes_once", fake_drain)

    task = await start_changes_drain_loop(interval=0.01)
    await drain_started.wait()
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass

    assert task.done()


@pytest.mark.asyncio
async def test_drain_loop_swallows_errors_and_keeps_running(monkeypatch):
    """A drain exception must not kill the loop — it logs and ticks again."""
    call_count = {"n": 0}

    async def flaky_drain(*, batch_size=100, publisher=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return {"published": 0, "failed": 0, "skipped_due_to_lock": False}

    monkeypatch.setattr("src.workers.changes_drain._drain_changes_once", flaky_drain)

    task = await start_changes_drain_loop(interval=0.01)
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert call_count["n"] >= 2, "loop must continue ticking after drain raises"


@pytest.mark.asyncio
async def test_drain_loop_reuses_single_publisher_across_ticks(monkeypatch):
    """Fast loop builds ONE publisher and reuses it across every tick.

    Constructing per-tick burns a Redis connection per fast tick (#154).
    Ownership lives in the loop and aclose() runs exactly once on cancel.
    """
    init_calls: list[None] = []
    close_calls: list[None] = []

    real_init = ChangePublisher.__init__
    real_aclose = ChangePublisher.aclose

    def counting_init(self, *, redis_client=None):
        init_calls.append(None)
        real_init(self, redis_client=redis_client)

    async def counting_aclose(self):
        close_calls.append(None)
        await real_aclose(self)

    seen_publishers: list[ChangePublisher] = []

    async def fake_drain(*, batch_size=100, publisher=None):
        # The loop must thread its publisher through every tick.
        assert publisher is not None, "fast loop must pass its publisher into _drain_changes_once"
        seen_publishers.append(publisher)
        return {"published": 0, "failed": 0, "skipped_due_to_lock": False}

    with patch.object(ChangePublisher, "__init__", counting_init):
        with patch.object(ChangePublisher, "aclose", counting_aclose):
            monkeypatch.setattr("src.workers.changes_drain._drain_changes_once", fake_drain)

            task = await start_changes_drain_loop(interval=0.01)
            try:
                # Allow several ticks.
                await asyncio.sleep(0.05)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    assert len(seen_publishers) >= 2, "loop must tick more than once for this test to be meaningful"
    assert all(p is seen_publishers[0] for p in seen_publishers), (
        "every tick must receive the same publisher instance"
    )
    assert len(init_calls) == 1, (
        f"publisher built {len(init_calls)}× — should be reused, not rebuilt"
    )
    assert len(close_calls) == 1, (
        f"publisher closed {len(close_calls)}× — should close exactly once on shutdown"
    )


@pytest.mark.asyncio
async def test_drain_changes_once_does_not_close_injected_publisher():
    """When a publisher is injected, ``_drain_changes_once`` must not close it.

    Ownership stays with the caller (the fast loop). Without injection,
    the function still builds + closes its own publisher (preserves the
    periodic worker's lifecycle — out of scope for #154).
    """
    from contextlib import asynccontextmanager

    fake = fakeredis_aio.FakeRedis()
    try:
        injected = ChangePublisher(redis_client=fake)
        close_calls = {"n": 0}
        real_aclose = injected.aclose

        async def counting_aclose():
            close_calls["n"] += 1
            await real_aclose()

        injected.aclose = counting_aclose  # type: ignore[method-assign]

        # Stub session factory: no rows, lock acquired, commit no-op.
        class _FakeSession:
            async def scalar(self, *_args, **_kwargs):
                return True

            async def commit(self):
                return None

        @asynccontextmanager
        async def _session_cm():
            yield _FakeSession()

        def _session_factory():
            return _session_cm()

        async def _no_rows(*_args, **_kwargs):
            return []

        with patch("src.workers.changes_drain.get_session_factory", return_value=_session_factory):
            with patch("src.workers.changes_drain.select_unpublished", _no_rows):
                result = await _drain_changes_once(publisher=injected)

        assert result["skipped_due_to_lock"] is False
        assert close_calls["n"] == 0, "injected publisher must not be closed by _drain_changes_once"
    finally:
        await fake.aclose()


@pytest.mark.asyncio
async def test_periodic_task_still_registered():
    """Sanity: the 1-minute periodic stays as a safety floor (not removed)."""
    from src.workers import get_app, reset_app

    reset_app()
    try:
        app = get_app()
        task_keys = {name for name, _ in app.periodic_registry.periodic_tasks}
        assert "drain_changes_outbox" in task_keys
    finally:
        reset_app()
