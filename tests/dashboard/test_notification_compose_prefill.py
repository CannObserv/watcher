"""Tests for compose-prefill HTMX endpoints.

GET /notifications/compose-body-prefill — returns composed Jinja string for
the body textarea based on current form state + preview_event.
GET /notifications/compose-title-prefill — returns default title Jinja.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.integration
class TestComposeTitlePrefill:
    async def test_returns_default_title_template(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-title-prefill",
            params={"preview_event": "change_detected"},
        )
        assert resp.status_code == 200
        # Default title: "{{ event_label }}: {{ watch_name }}"
        assert "{{ event_label }}" in resp.text
        assert "{{ watch_name }}" in resp.text

    async def test_falls_back_to_change_detected_for_unknown_event(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-title-prefill",
            params={"preview_event": "not_real"},
        )
        assert resp.status_code == 200
        assert "{{ event_label }}" in resp.text


@pytest.mark.integration
class TestComposeBodyPrefill:
    async def test_default_only_returns_event_default_body(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-body-prefill",
            params={"preview_event": "change_detected"},
        )
        assert resp.status_code == 200
        # Default body template for change_detected
        assert "{{ watch_url }}" in resp.text
        assert "{{ change_summary }}" in resp.text

    async def test_includes_enabled_additive_snippets(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-body-prefill",
            params={
                "preview_event": "change_detected",
                "content_config__include_domain": "1",
                "content_config__include_significance": "1",
            },
        )
        assert resp.status_code == 200
        assert "Domain:" in resp.text
        assert "Significance:" in resp.text

    async def test_omits_disabled_snippets(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-body-prefill",
            params={"preview_event": "change_detected"},
        )
        assert resp.status_code == 200
        assert "Domain:" not in resp.text
        assert "Significance:" not in resp.text

    async def test_watch_error_returns_its_default_body(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-body-prefill",
            params={"preview_event": "watch_error"},
        )
        assert resp.status_code == 200
        assert "HTTP" in resp.text
        assert "status_code" in resp.text
