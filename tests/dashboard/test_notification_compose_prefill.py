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
        # Default title: "{{ event_label }}: {{ item_name }}"
        assert "{{ event_label }}" in resp.text
        assert "{{ item_name }}" in resp.text

    async def test_falls_back_to_change_detected_for_unknown_event(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-title-prefill",
            params={"preview_event": "not_real"},
        )
        assert resp.status_code == 200
        assert "{{ event_label }}" in resp.text


@pytest.mark.integration
class TestComposeBodyPrefill:
    async def test_returns_full_default_body_skeleton(self, client: AsyncClient):
        """Seed button returns the change_detected default body template —
        the always-present skeleton (header + body block)."""
        resp = await client.get(
            "/notifications/compose-body-prefill",
            params={"preview_event": "change_detected"},
        )
        assert resp.status_code == 200
        assert "{{ item_url }}" in resp.text
        assert "{{ change_summary }}" in resp.text
        assert "{{ occurred_at_iso }}" in resp.text
        assert "WATCH:" in resp.text

    async def test_ignores_toggle_state(self, client: AsyncClient):
        """Toggles drive Python-side interleaving in build_body, not the seed
        template. The returned string is identical whether toggles are on or
        off — the user gets the skeleton to edit regardless."""
        plain = await client.get(
            "/notifications/compose-body-prefill",
            params={"preview_event": "change_detected"},
        )
        with_toggles = await client.get(
            "/notifications/compose-body-prefill",
            params={
                "preview_event": "change_detected",
                "content_config__include_domain": "1",
                "content_config__include_significance": "1",
            },
        )
        assert plain.status_code == 200
        assert with_toggles.status_code == 200
        assert plain.text == with_toggles.text

    async def test_watch_error_returns_its_default_body(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/compose-body-prefill",
            params={"preview_event": "watch_error"},
        )
        assert resp.status_code == 200
        assert "HTTP" in resp.text
        assert "status_code" in resp.text
