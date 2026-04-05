"""Unit tests for apprise_builder catalog + URL assembly."""

import apprise
import pytest

from src.core.notifications.apprise_builder import (
    assemble_url,
    get_plugin_detail,
    list_plugins,
)


class TestListPlugins:
    def test_returns_list(self):
        plugins = list_plugins()
        assert isinstance(plugins, list)
        assert len(plugins) > 100

    def test_sorted_by_service_name(self):
        plugins = list_plugins()
        names = [p["service_name"] for p in plugins]
        assert names == sorted(names, key=str.lower)

    def test_contains_discord(self):
        plugins = list_plugins()
        schemas = [p["plugin_schema"] for p in plugins]
        assert "discord" in schemas

    def test_each_item_has_required_keys(self):
        plugins = list_plugins()
        for p in plugins:
            assert "plugin_schema" in p
            assert "service_name" in p
            assert "category" in p


class TestGetPluginDetail:
    def test_returns_detail_for_discord(self):
        detail = get_plugin_detail("discord")
        assert detail is not None
        assert detail["plugin_schema"] == "discord"
        assert detail["service_name"] == "Discord"

    def test_discord_has_required_tokens(self):
        detail = get_plugin_detail("discord")
        assert "webhook_id" in detail["tokens"]
        assert detail["tokens"]["webhook_id"]["required"] is True
        assert detail["tokens"]["webhook_id"]["private"] is True

    def test_schema_token_excluded(self):
        detail = get_plugin_detail("discord")
        assert "schema" not in detail["tokens"]

    def test_unknown_schema_returns_none(self):
        assert get_plugin_detail("notaschema") is None

    def test_slack_has_two_variants(self):
        detail = get_plugin_detail("slack")
        assert len(detail["variants"]) == 2

    def test_discord_has_no_variants(self):
        detail = get_plugin_detail("discord")
        assert detail["variants"] == []

    def test_alias_tokens_excluded(self):
        # Alias tokens (those with alias_of key) should not appear in the token dict.
        # Discord has none, but Slack has aliases like 'access', 'secret', 'to', etc.
        detail = get_plugin_detail("slack")
        for name, tok in detail["tokens"].items():
            assert "alias_of" not in tok

    def test_variant_index_out_of_range_falls_back(self):
        # Out-of-range variant_index should not raise; falls back to all templates.
        url = assemble_url(
            "discord",
            {"webhook_id": "abc123", "webhook_token": "xyz789"},
            variant_index=99,
        )
        assert url.startswith("discord://")


class TestAssembleUrl:
    def test_discord_assembles_correctly(self):
        url = assemble_url("discord", {"webhook_id": "abc123", "webhook_token": "xyz789"})
        assert url.startswith("discord://abc123/xyz789")

    def test_assembled_url_is_valid_apprise_url(self):
        url = assemble_url("discord", {"webhook_id": "abc123", "webhook_token": "xyz789"})
        ap = apprise.Apprise()
        assert ap.add(url)

    def test_missing_required_token_raises_value_error(self):
        with pytest.raises(ValueError, match="required"):
            assemble_url("discord", {"webhook_id": "abc123"})

    def test_unknown_schema_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown"):
            assemble_url("notaschema", {})

    def test_slack_legacy_variant(self):
        url = assemble_url(
            "slack",
            {"token_a": "T111", "token_b": "B222", "token_c": "C333"},
            variant_index=0,
        )
        assert url.startswith("slack://")
        assert "T111" in url

    def test_slack_bot_token_variant(self):
        url = assemble_url(
            "slack",
            {"access_token": "xoxb-abc"},
            variant_index=1,
        )
        assert url.startswith("slack://")
        assert "xoxb-abc" in url
