"""Unit tests for URL probe logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.egress import DestinationRefused, GuardedTransport
from src.core.probe import ProbeResult, build_probe_client, probe_url
from tests.core.egress_fakes import (
    PUBLIC_ADDRESS,
    RecordingTransport,
    never_resolves,
    resolver,
)


class TestProbeResult:
    def test_probe_result_fields(self):
        r = ProbeResult(
            effective_url="https://example.com/page",
            effective_domain="example.com",
            redirect_chain=["https://www.example.com/page", "https://example.com/page"],
            status_code=200,
            content_type="text/html; charset=utf-8",
        )
        assert r.effective_domain == "example.com"
        assert len(r.redirect_chain) == 2


class TestProbeUrl:
    async def test_no_redirect(self):
        mock_response = MagicMock()
        mock_response.url = httpx.URL("https://example.com/page")
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}
        mock_response.history = []

        with patch("src.core.probe.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.head = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await probe_url("https://example.com/page")

        assert result.effective_url == "https://example.com/page"
        assert result.effective_domain == "example.com"
        assert result.redirect_chain == ["https://example.com/page"]
        assert result.status_code == 200

    async def test_redirect_followed(self):
        redirect_response = MagicMock()
        redirect_response.url = httpx.URL("https://www.example.com/page")
        redirect_response.status_code = 301

        final_response = MagicMock()
        final_response.url = httpx.URL("https://example.com/page")
        final_response.status_code = 200
        final_response.headers = {"content-type": "text/html"}
        final_response.history = [redirect_response]

        with patch("src.core.probe.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.head = AsyncMock(return_value=final_response)
            mock_client_cls.return_value = mock_client

            result = await probe_url("https://www.example.com/page")

        assert result.effective_url == "https://example.com/page"
        assert result.effective_domain == "example.com"
        assert result.redirect_chain == [
            "https://www.example.com/page",
            "https://example.com/page",
        ]

    async def test_connection_error_raises(self):
        with patch("src.core.probe.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.head = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.ConnectError):
                await probe_url("https://unreachable.example.com/")


class TestProbeDestinationGuard:
    """probe_url runs behind the #305 destination guard."""

    async def test_loopback_url_is_refused_before_any_request(self):
        inner = RecordingTransport()

        with patch(
            "src.core.probe.build_probe_client",
            lambda: build_probe_client(inner, resolve=never_resolves()),
        ):
            with pytest.raises(DestinationRefused):
                await probe_url("http://127.0.0.1:9999/")

        assert inner.requests == []

    async def test_public_url_redirecting_into_loopback_is_refused(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/"})

        with patch(
            "src.core.probe.build_probe_client",
            lambda: build_probe_client(
                httpx.MockTransport(handler),
                resolve=resolver({"origin.example.com": [PUBLIC_ADDRESS]}),
            ),
        ):
            with pytest.raises(DestinationRefused):
                await probe_url("https://origin.example.com/")

        assert seen == ["https://origin.example.com/"]

    async def test_public_url_still_probes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "www.example.com":
                return httpx.Response(301, headers={"location": "https://example.com/page"})
            return httpx.Response(200, headers={"content-type": "text/html"})

        with patch(
            "src.core.probe.build_probe_client",
            lambda: build_probe_client(
                httpx.MockTransport(handler),
                resolve=resolver(
                    {"www.example.com": [PUBLIC_ADDRESS], "example.com": [PUBLIC_ADDRESS]}
                ),
            ),
        ):
            result = await probe_url("https://www.example.com/page")

        assert result.effective_url == "https://example.com/page"
        assert result.effective_domain == "example.com"
        assert result.redirect_chain == [
            "https://www.example.com/page",
            "https://example.com/page",
        ]
        assert result.status_code == 200
        assert result.content_type == "text/html"

    async def test_the_default_client_carries_the_guard(self):
        """No seam: the client probe_url actually builds is a guarded one."""
        client = build_probe_client()
        try:
            assert isinstance(client._transport, GuardedTransport)
            assert client.follow_redirects is True
        finally:
            await client.aclose()
