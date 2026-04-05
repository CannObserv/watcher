"""Tests for GET /api/v1/apprise/plugins endpoints."""


class TestListApprisePlugins:
    async def test_returns_200(self, client):
        resp = await client.get("/api/v1/apprise/plugins")
        assert resp.status_code == 200

    async def test_returns_list(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        assert isinstance(data, list)
        assert len(data) > 100

    async def test_sorted_by_service_name(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        names = [p["service_name"] for p in data]
        assert names == sorted(names, key=str.lower)

    async def test_contains_discord(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        schemas = [p["plugin_schema"] for p in data]
        assert "discord" in schemas

    async def test_item_shape(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        item = next(p for p in data if p["plugin_schema"] == "discord")
        assert set(item.keys()) >= {"plugin_schema", "service_name", "category"}


class TestGetApprisePlugin:
    async def test_discord_returns_200(self, client):
        resp = await client.get("/api/v1/apprise/plugins/discord")
        assert resp.status_code == 200

    async def test_discord_response_shape(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert data["plugin_schema"] == "discord"
        assert data["service_name"] == "Discord"
        assert "tokens" in data
        assert "variants" in data

    async def test_discord_has_webhook_id_token(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert "webhook_id" in data["tokens"]
        assert data["tokens"]["webhook_id"]["required"] is True
        assert data["tokens"]["webhook_id"]["private"] is True

    async def test_discord_no_schema_token(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert "schema" not in data["tokens"]

    async def test_unknown_schema_returns_404(self, client):
        resp = await client.get("/api/v1/apprise/plugins/notaschema")
        assert resp.status_code == 404

    async def test_slack_has_two_variants(self, client):
        data = (await client.get("/api/v1/apprise/plugins/slack")).json()
        assert len(data["variants"]) == 2

    async def test_discord_has_no_variants(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert data["variants"] == []
