"""Tests for the FastAPI lifespan.

Nothing to pre-warm since #254: the Archiver SDK went with Watcher's last
outbound call to Archiver, so what remains to assert is the consumers — which
start, and that they stop.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_does_not_start_changes_drain(monkeypatch):
    """Lifespan should not start a changes-drain loop after Phase 5 cutover."""
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(install_signal_handlers: bool = True) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
    ):
        from src.api.main import app

        async with app.router.lifespan_context(app):
            for attr in dir(app.state):
                assert "changes_drain" not in attr.lower()
                assert "drain_changes" not in attr.lower()


@pytest.mark.asyncio
async def test_registry_consumer_is_dormant_without_a_bus_url(monkeypatch):
    """Done-when #1 (#254): no ``WATCHER_BUS_REDIS_URL`` → no consumer at all.

    Dormant, not degraded-and-noisy: the process still serves, and the error log
    beside it says why the registry cannot reconcile.
    """
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(install_signal_handlers: bool = True) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run
    start_registry = MagicMock()

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
        patch("src.api.main.start_registry_consumer", start_registry),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass

    start_registry.assert_not_called()


@pytest.mark.asyncio
async def test_registry_consumer_starts_and_stops_with_a_bus_url(monkeypatch):
    """With a bus, the registry inbox starts beside the fact inbox and is
    cancelled on shutdown — an un-cancelled task would hold the process open for
    a full read block."""
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(install_signal_handlers: bool = True) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    async def _forever() -> None:
        await asyncio.Event().wait()

    registry_task = asyncio.create_task(_forever())
    blobs_task = asyncio.create_task(_forever())

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=MagicMock()),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
        patch("src.api.main.start_blobs_consumer", MagicMock(return_value=blobs_task)),
        patch("src.api.main.start_registry_consumer", MagicMock(return_value=registry_task)),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass

    assert registry_task.cancelled()
    assert blobs_task.cancelled()
