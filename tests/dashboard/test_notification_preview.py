"""Tests for POST /notifications/preview — stateless live-preview endpoint."""

import pytest
from httpx import AsyncClient


@pytest.mark.integration
class TestPreviewEndpoint:
    async def test_returns_200_with_default_event(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={"preview_event": "change_detected"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        body = resp.text
        # Default title template: "Change Detected: Example Watch"
        assert "Change Detected" in body
        assert "Example Watch" in body

    async def test_default_body_contains_change_summary(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={"preview_event": "change_detected"},
        )
        assert resp.status_code == 200
        # Mock fixture has 1 added, 1 modified, 1 removed
        assert "1 added, 1 modified, 1 removed" in resp.text

    async def test_includes_toggle_driven_slot_when_toggle_on(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={
                "preview_event": "change_detected",
                "content_config__include_domain": "1",
            },
        )
        assert resp.status_code == 200
        assert "DOMAIN: example.com" in resp.text

    async def test_user_body_template_renders(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={
                "preview_event": "change_detected",
                "content_config__body_template": "Hi {{ watch_name }}!",
            },
        )
        assert resp.status_code == 200
        assert "Hi Example Watch!" in resp.text

    async def test_user_title_template_renders(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={
                "preview_event": "change_detected",
                "content_config__title_template": "[{{ watch_name }}]",
            },
        )
        assert resp.status_code == 200
        assert "[Example Watch]" in resp.text

    async def test_bad_title_template_renders_error_card(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={
                "preview_event": "change_detected",
                "content_config__title_template": "{{ unknown_var_xyz }}",
            },
        )
        assert resp.status_code == 200
        assert "Template error" in resp.text
        assert "title template" in resp.text

    async def test_bad_body_template_renders_error_card(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={
                "preview_event": "change_detected",
                "content_config__body_template": "{{ unknown_var_xyz }}",
            },
        )
        assert resp.status_code == 200
        assert "Template error" in resp.text
        assert "body template" in resp.text

    async def test_invalid_preview_event_falls_back_to_change_detected(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={"preview_event": "not_a_real_event"},
        )
        assert resp.status_code == 200
        # Should render change_detected default
        assert "Change Detected" in resp.text

    async def test_per_event_override_applied_when_event_overridden(self, client: AsyncClient):
        """Override for change_detected should be picked up by resolve_options."""
        resp = await client.post(
            "/notifications/preview",
            data={
                "preview_event": "change_detected",
                "content_config__override__change_detected__include_significance": "1",
            },
        )
        assert resp.status_code == 200
        # Mock significance is 0.65 → 65%
        assert "SIGNIFICANCE: 65%" in resp.text

    async def test_returns_html_fragment_not_full_page(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            data={"preview_event": "change_detected"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Fragment should not contain <html> or <body> tags
        assert "<html" not in resp.text.lower()
        assert "<!doctype" not in resp.text.lower()

    async def test_preview_body_and_title_have_break_all_for_long_urls(self, client: AsyncClient):
        """Issue #106 — long URLs in title/body must wrap inside the preview card.

        whitespace-pre-wrap preserves newlines but will not break unspaced URLs.
        The body <pre> and the title <div> must include `break-all` so a long
        URL wraps instead of overflowing the card width.
        """
        resp = await client.post(
            "/notifications/preview",
            data={"preview_event": "change_detected"},
        )
        assert resp.status_code == 200
        body = resp.text
        # Body <pre> tag must include break-all alongside whitespace-pre-wrap.
        assert "whitespace-pre-wrap" in body
        assert "break-all" in body
        # And specifically: the body <pre> still has whitespace-pre-wrap so
        # newlines are preserved.
        assert 'class="whitespace-pre-wrap' in body or "whitespace-pre-wrap " in body
