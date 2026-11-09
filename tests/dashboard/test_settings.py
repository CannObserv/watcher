"""Integration tests for the /settings index page."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_settings_index_links_api_keys_and_no_notifications(client: AsyncClient):
    """GET /settings — the API keys card, and no Notification Templates card:
    the template library went with the notification subsystem (#379)."""
    resp = await client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert 'href="/settings/api-keys"' in body
    assert 'href="/notifications"' not in body
    assert "Notification Templates" not in body


async def test_the_template_library_is_gone(client: AsyncClient):
    """#379: the dashboard's notification routes are deleted, not hidden."""
    for path in ("/notifications", "/notifications/new"):
        assert (await client.get(path)).status_code == 404, path
