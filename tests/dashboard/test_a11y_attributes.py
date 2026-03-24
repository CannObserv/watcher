"""Tests for accessibility attributes in dashboard templates."""

import pytest

pytestmark = pytest.mark.integration


class TestAccessibility:
    async def test_skip_link_present(self, client):
        """Dashboard pages include a skip-to-content link."""
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'href="#main-content"' in resp.text
        assert "Skip to main content" in resp.text

    async def test_main_landmark_present(self, client):
        """Dashboard pages have a main landmark with correct id."""
        resp = await client.get("/")
        assert 'id="main-content"' in resp.text

    async def test_nav_landmark_has_aria_label(self, client):
        """Navigation landmark has an aria-label."""
        resp = await client.get("/")
        assert 'aria-label="Main navigation"' in resp.text

    async def test_html_lang_and_dir(self, client):
        """HTML element has lang and dir attributes."""
        resp = await client.get("/")
        assert 'lang="en"' in resp.text
        assert 'dir="ltr"' in resp.text

    async def test_htmx_swap_targets_have_live_region(self, client):
        """HTMX swap targets on dashboard have aria-live attributes."""
        resp = await client.get("/")
        assert 'aria-live="polite"' in resp.text

    async def test_decorative_emoji_hidden(self, client):
        """Decorative emojis are wrapped in aria-hidden."""
        resp = await client.get("/")
        assert 'aria-hidden="true">🌱🏛️🔍</span>' in resp.text

    async def test_dark_mode_toggle_has_aria_label(self, client):
        """Dark mode toggle button has an aria-label."""
        resp = await client.get("/")
        assert 'id="theme-toggle"' in resp.text
        assert "aria-label" in resp.text
