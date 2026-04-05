"""Unit tests for the apprise plugin form partial route."""

from unittest.mock import MagicMock

from httpx import ASGITransport, AsyncClient


async def _get(schema: str | None = None, raw: bool = False, variant: int = 0):
    from src.api.dependencies import get_db_session
    from src.api.main import app

    async def override_session():
        yield MagicMock()

    app.dependency_overrides[get_db_session] = override_session
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
        app.dependency_overrides.clear()


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

    async def test_raw_mode_returns_url_input(self):
        resp = await _get(raw=True)
        assert resp.status_code == 200
        assert 'name="apprise_url"' in resp.text
