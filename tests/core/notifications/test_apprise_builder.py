"""Unit tests for apprise_builder catalog + URL assembly."""

import apprise
import pytest

from src.core.notifications.apprise_builder import (
    assemble_url,
    get_plugin_detail,
    get_service_name,
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

    def test_items_include_setup_url(self):
        plugins = list_plugins()
        discord = next(p for p in plugins if p["plugin_schema"] == "discord")
        assert "setup_url" in discord
        assert discord["setup_url"].startswith("http")

    def test_items_include_service_url(self):
        plugins = list_plugins()
        discord = next(p for p in plugins if p["plugin_schema"] == "discord")
        assert "service_url" in discord
        assert discord["service_url"].startswith("http")


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

    def test_detail_includes_setup_url(self):
        detail = get_plugin_detail("discord")
        assert "setup_url" in detail
        assert detail["setup_url"].startswith("http")

    def test_detail_includes_service_url(self):
        detail = get_plugin_detail("discord")
        assert "service_url" in detail
        assert detail["service_url"].startswith("http")

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


class TestAssembleUrlMailgunRegression:
    """Regression tests for caronc/apprise#1576.

    Apprise's Mailgun (and SparkPost, SMTP2Go) templates place the API key in
    the URL port position: {schema}://{user}@{host}:{apikey}/{targets}
    Python's URL parser silently drops non-numeric port values, so without
    correction the key is read back as None and the URL fails to load.
    """

    def test_mailgun_url_is_loadable_by_apprise(self):
        url = assemble_url(
            "mailgun",
            {
                "user": "postmaster",
                "host": "mail.example.com",
                "apikey": "abc123def456",
                "target_email": "recipient@example.com",
            },
        )
        ap = apprise.Apprise()
        assert ap.add(url), f"Apprise could not load assembled Mailgun URL: {url}"

    def test_mailgun_url_contains_slash_not_colon_before_apikey(self):
        # The raw Apprise template uses {host}:{apikey} (port position).
        # We must normalise to {host}/{apikey} (path segment) so the key
        # is not silently dropped by the URL parser.
        url = assemble_url(
            "mailgun",
            {
                "user": "postmaster",
                "host": "mail.example.com",
                "apikey": "abc123def456",
            },
        )
        assert "mail.example.com/abc123def456" in url, (
            f"apikey should follow host as a path segment, not a port: {url}"
        )
        assert "mail.example.com:abc123def456" not in url

    def test_mailgun_target_email_map_to_targets(self):
        # Apprise defines target_email with map_to=targets. Submitting
        # target_email must substitute into {targets} in the template.
        url = assemble_url(
            "mailgun",
            {
                "user": "postmaster",
                "host": "mail.example.com",
                "apikey": "abc123def456",
                "target_email": "recipient@example.com",
            },
        )
        assert "recipient%40example.com" in url, (
            f"target_email should appear percent-encoded in URL path: {url}"
        )

    def test_mailgun_at_sign_in_target_is_percent_encoded(self):
        # Unencoded @ in a URL path segment corrupts URL structure.
        url = assemble_url(
            "mailgun",
            {
                "user": "postmaster",
                "host": "mail.example.com",
                "apikey": "abc123def456",
                "target_email": "recipient@example.com",
            },
        )
        # After the host/apikey portion, @ must be encoded
        path_part = url.split("abc123def456", 1)[-1]
        assert "@" not in path_part, f"@ in target email must be percent-encoded in path: {url}"

    def test_sparkpost_url_is_loadable_by_apprise(self):
        # SparkPost uses the same broken {host}:{apikey} template pattern.
        url = assemble_url(
            "sparkpost",
            {
                "user": "postmaster",
                "host": "sp.example.com",
                "apikey": "abc123def456",
                "target_email": "recipient@example.com",
            },
        )
        ap = apprise.Apprise()
        assert ap.add(url), f"Apprise could not load assembled SparkPost URL: {url}"

    def test_smtp2go_url_is_loadable_by_apprise(self):
        # SMTP2Go uses the same broken {host}:{apikey} template pattern.
        url = assemble_url(
            "smtp2go",
            {
                "user": "postmaster",
                "host": "smtp2go.example.com",
                "apikey": "abc123def456",
                "target_email": "recipient@example.com",
            },
        )
        ap = apprise.Apprise()
        assert ap.add(url), f"Apprise could not load assembled SMTP2Go URL: {url}"


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


class TestGetServiceName:
    def test_known_schema_returns_service_name(self):
        name = get_service_name("discord")
        assert name == "Discord"

    def test_unknown_schema_returns_schema(self):
        name = get_service_name("notaschema")
        assert name == "notaschema"

    def test_slack_returns_slack(self):
        name = get_service_name("slack")
        assert name == "Slack"
