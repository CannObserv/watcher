"""Tests for POST /api/v1/tools/propose-selectors."""

import pytest

from src.core.fetchers.base import FetchResult
from src.information.api.deps import get_http_fetcher
from src.information.api.main import app

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _stub_fetcher(content: bytes):
    class _Stub:
        async def fetch(self, url: str, config: dict | None = None):
            return FetchResult(
                content=content,
                status_code=200,
                headers={"content-type": "text/html"},
                duration_ms=5,
                fetcher_used="http",
            )

    return _Stub()


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_http_fetcher, None)


@pytest.mark.asyncio
async def test_propose_selectors_returns_ranked_candidates(client):
    html = b"""
    <html><body>
        <h1 class="page-title">Active Cannabis Licenses</h1>
        <div class="hash-abc12345xyz">Active Cannabis Licenses</div>
        <p>Other content</p>
    </body></html>
    """
    app.dependency_overrides[get_http_fetcher] = lambda: _stub_fetcher(html)
    response = await client.post(
        "/api/v1/tools/propose-selectors",
        headers=HEADERS,
        json={"url": "https://example.com", "description": "Active Cannabis Licenses"},
    )
    assert response.status_code == 200
    candidates = response.json()
    assert isinstance(candidates, list)
    assert len(candidates) >= 2
    # Each candidate must carry a selector, sample text, and a stability score in [0, 1].
    for c in candidates:
        assert "selector" in c
        assert "sample_text" in c
        assert "stability_score" in c
        assert 0.0 <= c["stability_score"] <= 1.0


@pytest.mark.asyncio
async def test_propose_selectors_penalises_volatile_classes(client):
    html = b"""
    <html><body>
        <h1 class="page-title">Active Cannabis Licenses</h1>
        <div class="hash-abc12345xyz">Active Cannabis Licenses</div>
    </body></html>
    """
    app.dependency_overrides[get_http_fetcher] = lambda: _stub_fetcher(html)
    response = await client.post(
        "/api/v1/tools/propose-selectors",
        headers=HEADERS,
        json={"url": "https://example.com", "description": "Active Cannabis Licenses"},
    )
    candidates = response.json()
    # Find the candidate whose selector references the volatile class and the one
    # that uses the stable .page-title; the stable one should outrank the
    # volatile one.
    by_volatility = sorted(candidates, key=lambda c: c["stability_score"], reverse=True)
    top = by_volatility[0]["selector"]
    assert "page-title" in top or top.startswith("h1")


@pytest.mark.asyncio
async def test_propose_selectors_empty_match_returns_empty_list(client):
    html = b"<html><body><p>nothing relevant</p></body></html>"
    app.dependency_overrides[get_http_fetcher] = lambda: _stub_fetcher(html)
    response = await client.post(
        "/api/v1/tools/propose-selectors",
        headers=HEADERS,
        json={"url": "https://example.com", "description": "missing target"},
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_propose_selectors_respects_top_k(client):
    # Five matching <p> elements; top_k=2 should clamp the output.
    html = (
        b"<html><body>"
        + b"".join(f"<p class='match-{i}'>target</p>".encode() for i in range(5))
        + b"</body></html>"
    )
    app.dependency_overrides[get_http_fetcher] = lambda: _stub_fetcher(html)
    response = await client.post(
        "/api/v1/tools/propose-selectors",
        headers=HEADERS,
        json={
            "url": "https://example.com",
            "description": "target",
            "top_k": 2,
        },
    )
    assert response.status_code == 200
    candidates = response.json()
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_propose_selectors_requires_api_key(client):
    response = await client.post(
        "/api/v1/tools/propose-selectors",
        json={"url": "https://example.com", "description": "anything"},
    )
    assert response.status_code == 403
