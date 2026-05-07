"""Tests for the FastAPI lifespan: pre-warm + clean shutdown of ArchiverClient."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_dummy_task() -> asyncio.Task:
    """Build a real asyncio.Task that awaits forever — gathered+cancelled by lifespan."""

    async def _hang() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    return asyncio.create_task(_hang())


@pytest.mark.asyncio
async def test_lifespan_prewarms_archiver_client(monkeypatch):
    """Lifespan startup must call get_archiver_client (pre-warm)."""
    monkeypatch.setenv("ARCHIVER_API_KEY", "test-key")

    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()

    fake_reg = MagicMock()
    fake_reg.get_archiver_client = MagicMock(return_value=fake_client)
    fake_reg.aclose_archiver_client = AsyncMock()

    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(install_signal_handlers: bool = True) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    poller_task = _make_dummy_task()

    with (
        patch("src.api.main.get_registry", return_value=fake_reg),
        patch("src.api.main.start_config_poller", AsyncMock(return_value=poller_task)),
        patch("src.api.main.hydrate_rate_limiter", AsyncMock()),
        patch("src.workers.get_app", return_value=fake_proc_app),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass

    fake_reg.get_archiver_client.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_closes_archiver_client_on_shutdown(monkeypatch):
    """Lifespan exit must call reg.aclose_archiver_client (clean shutdown)."""
    monkeypatch.setenv("ARCHIVER_API_KEY", "test-key")

    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()

    fake_reg = MagicMock()
    fake_reg.get_archiver_client = MagicMock(return_value=fake_client)
    fake_reg.aclose_archiver_client = AsyncMock()

    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(install_signal_handlers: bool = True) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    poller_task = _make_dummy_task()

    with (
        patch("src.api.main.get_registry", return_value=fake_reg),
        patch("src.api.main.start_config_poller", AsyncMock(return_value=poller_task)),
        patch("src.api.main.hydrate_rate_limiter", AsyncMock()),
        patch("src.workers.get_app", return_value=fake_proc_app),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass

    fake_reg.aclose_archiver_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_closes_client_after_proc_app(monkeypatch):
    """SDK aclose must run AFTER the procrastinate app closes (last shutdown step)."""
    monkeypatch.setenv("ARCHIVER_API_KEY", "test-key")

    call_order: list[str] = []

    fake_reg = MagicMock()
    fake_reg.get_archiver_client = MagicMock()

    async def _aclose_info():
        call_order.append("aclose_archiver_client")

    fake_reg.aclose_archiver_client = AsyncMock(side_effect=_aclose_info)

    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()

    async def _close_proc():
        call_order.append("proc_app.close_async")

    fake_proc_app.close_async = AsyncMock(side_effect=_close_proc)

    async def _worker_run(install_signal_handlers: bool = True) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    poller_task = _make_dummy_task()

    with (
        patch("src.api.main.get_registry", return_value=fake_reg),
        patch("src.api.main.start_config_poller", AsyncMock(return_value=poller_task)),
        patch("src.api.main.hydrate_rate_limiter", AsyncMock()),
        patch("src.workers.get_app", return_value=fake_proc_app),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass

    assert call_order == ["proc_app.close_async", "aclose_archiver_client"]
