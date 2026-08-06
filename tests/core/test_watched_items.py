"""Tests for resolve_watch_target — the async-create mode branch (#241 step 3)."""

from unittest.mock import AsyncMock

import pytest

from src.core.fetch_commands import FETCH_MODE_ENV
from src.core.models.watched_item import WatchHealthStatus
from src.core.probe import ProbeResult
from src.core.watched_items import resolve_watch_target


def _probe_result(url: str) -> ProbeResult:
    return ProbeResult(
        effective_url=f"{url}/resolved",
        effective_domain="resolved.example",
        redirect_chain=[url],
        status_code=200,
        content_type="text/html",
    )


class TestResolveWatchTarget:
    async def test_local_mode_probes_inline(self, monkeypatch):
        monkeypatch.delenv(FETCH_MODE_ENV, raising=False)
        probe = AsyncMock(side_effect=_probe_result)

        effective_url, domain, health = await resolve_watch_target(
            "https://lcb.wa.gov/notices", probe
        )

        probe.assert_awaited_once_with("https://lcb.wa.gov/notices")
        assert effective_url == "https://lcb.wa.gov/notices/resolved"
        assert domain == "resolved.example"
        assert health == WatchHealthStatus.UNKNOWN

    async def test_bus_mode_defers_the_probe_to_the_first_fetch(self, monkeypatch):
        monkeypatch.setenv(FETCH_MODE_ENV, "bus")
        probe = AsyncMock(side_effect=AssertionError("bus mode must not probe"))

        effective_url, domain, health = await resolve_watch_target(
            "https://LCB.wa.gov/Notices", probe
        )

        probe.assert_not_awaited()
        assert effective_url == "https://LCB.wa.gov/Notices"  # submitted URL, untouched
        assert domain == "lcb.wa.gov"  # urlparse().hostname — the limiter-key derivation
        assert health == WatchHealthStatus.PROBING


class TestBusModeUrlValidation:
    """CR-3: syntactic validation stays at the boundary in bus mode."""

    async def test_rejects_a_schemeless_url(self, monkeypatch):
        monkeypatch.setenv(FETCH_MODE_ENV, "bus")
        probe = AsyncMock()
        with pytest.raises(ValueError, match="invalid URL"):
            await resolve_watch_target("not a url", probe)
        probe.assert_not_awaited()

    async def test_rejects_a_non_http_scheme(self, monkeypatch):
        monkeypatch.setenv(FETCH_MODE_ENV, "bus")
        with pytest.raises(ValueError, match="invalid URL"):
            await resolve_watch_target("ftp://old.example/file", AsyncMock())

    async def test_rejects_a_hostless_url(self, monkeypatch):
        monkeypatch.setenv(FETCH_MODE_ENV, "bus")
        with pytest.raises(ValueError, match="invalid URL"):
            await resolve_watch_target("https:///nopath-host", AsyncMock())
