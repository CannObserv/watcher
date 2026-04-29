"""Unit tests for the apprise plugin form partial route."""

from unittest.mock import MagicMock

from httpx import ASGITransport, AsyncClient

from src.api.deps import require_api_key
from src.core.models.app_user import AppUser
from src.dashboard.deps import get_dashboard_user


async def _get(schema: str | None = None, raw: bool = False, variant: int = 0):
    from src.api.deps import get_db_session
    from src.api.main import app

    async def override_session():
        yield MagicMock()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_dashboard_user] = lambda: AppUser(
        id="test-user-id", email="test@example.com"
    )
    app.dependency_overrides[require_api_key] = lambda: "test-user-id"
    try:
        params = {}
        if schema:
            params["schema"] = schema
        if raw:
            params["raw"] = "1"
        if variant:
            params["variant"] = str(variant)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/partials/apprise-plugin-form", params=params)
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_dashboard_user, None)
        app.dependency_overrides.pop(require_api_key, None)


class TestApprisePluginFormPartial:
    async def test_discord_returns_200(self):
        resp = await _get(schema="discord")
        assert resp.status_code == 200

    async def test_unknown_schema_returns_404(self):
        resp = await _get(schema="notaschema")
        assert resp.status_code == 404

    async def test_discord_has_hidden_schema_input(self):
        resp = await _get(schema="discord")
        assert 'name="plugin_schema"' in resp.text
        assert 'value="discord"' in resp.text

    async def test_discord_webhook_id_is_password_input(self):
        resp = await _get(schema="discord")
        # webhook_id is private=True → type="password"
        assert 'name="tok_webhook_id"' in resp.text
        assert 'type="password"' in resp.text

    async def test_discord_optional_token_in_advanced_section(self):
        resp = await _get(schema="discord")
        # botname is optional — should appear in advanced disclosure
        assert "tok_botname" in resp.text

    async def test_slack_has_variant_selector(self):
        resp = await _get(schema="slack")
        # Slack has 2 variants — variant selector must be present
        assert "variant" in resp.text.lower()

    async def test_discord_shows_setup_guide_link(self):
        resp = await _get(schema="discord")
        assert resp.status_code == 200
        assert "Setup guide" in resp.text

    async def test_slack_variant0_shows_webhook_tokens(self):
        resp = await _get(schema="slack", variant=0)
        assert resp.status_code == 200
        assert 'name="tok_token_a"' in resp.text
        assert 'name="tok_token_b"' in resp.text
        assert 'name="tok_token_c"' in resp.text
        # access_token belongs to variant 1 — must be hidden
        assert 'name="tok_access_token"' not in resp.text
        # optional tokens shared across variants survive filtering
        assert "tok_targets" in resp.text

    async def test_slack_variant1_shows_oauth_token(self):
        resp = await _get(schema="slack", variant=1)
        assert resp.status_code == 200
        assert 'name="tok_access_token"' in resp.text
        # webhook tokens belong to variant 0 — must be hidden
        assert 'name="tok_token_a"' not in resp.text
        assert 'name="tok_token_b"' not in resp.text
        assert 'name="tok_token_c"' not in resp.text

    async def test_raw_mode_returns_url_input(self):
        resp = await _get(raw=True)
        assert resp.status_code == 200
        assert 'name="apprise_url"' in resp.text

    async def test_raw_mode_shows_apprise_doc_links(self):
        resp = await _get(raw=True)
        assert resp.status_code == 200
        assert "appriseit.com/getting-started/universal-syntax/" in resp.text
        assert "appriseit.com/tools/url-builder/" in resp.text
