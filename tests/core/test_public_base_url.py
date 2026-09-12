"""Tests for the dashboard's public base URL (#296 D6).

Notification links used to be built from a constant naming the shared VM. The
move to ``co-watcher`` changes the host, and a hardcoded host is also why a dev
server's notifications linked into production. The base is configuration now,
and an unset one means "no link" — never a guessed host.
"""

import logging

import pytest

from src.core.public_base_url import (
    PUBLIC_BASE_URL_ENV,
    PublicBaseUrlInvalid,
    assert_public_base_url,
    public_base_url,
)


class TestPublicBaseUrl:
    def test_unset_means_no_base(self) -> None:
        """No default host, deliberately: a default is a guess, and a guessed
        host is how links point at a VM that no longer serves the dashboard."""
        assert public_base_url({}) is None

    def test_blank_means_no_base(self) -> None:
        assert public_base_url({PUBLIC_BASE_URL_ENV: "   "}) is None

    def test_https_base_is_returned(self) -> None:
        env = {PUBLIC_BASE_URL_ENV: "https://co-watcher.exe.xyz"}
        assert public_base_url(env) == "https://co-watcher.exe.xyz"

    def test_trailing_slash_is_stripped(self) -> None:
        """Every caller appends ``/watched-items/…``; a trailing slash here
        would double it."""
        env = {PUBLIC_BASE_URL_ENV: "https://co-watcher.exe.xyz/"}
        assert public_base_url(env) == "https://co-watcher.exe.xyz"

    def test_a_port_and_a_path_prefix_survive(self) -> None:
        """The dev server lives on :8001, and a dashboard behind a path prefix
        is a legitimate deployment shape."""
        assert (
            public_base_url({PUBLIC_BASE_URL_ENV: "https://co-watcher.exe.xyz:8001"})
            == "https://co-watcher.exe.xyz:8001"
        )
        assert (
            public_base_url({PUBLIC_BASE_URL_ENV: "https://example.test/watcher/"})
            == "https://example.test/watcher"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "co-watcher.exe.xyz",  # no scheme: the likeliest typo
            "ftp://co-watcher.exe.xyz",
            "https://",  # no host
            "https://co-watcher.exe.xyz?next=/",  # a query would be appended into
            "https://co-watcher.exe.xyz#top",
            "https://co-watcher.exe.xyz/?",  # bare: urlsplit reports an empty query
            "https://co-watcher.exe.xyz/#",
            "https://[co-watcher.exe.xyz",  # urlsplit raises ValueError itself
            "https://co-watcher.exe.xyz:abc",  # .port raises; unread, it passed
            "https://co-watcher.exe.xyz:99999",
            "https://co watcher.exe.xyz",  # whitespace inside survives urlsplit
            "https://co-watcher.exe.xyz/a\tb",
        ],
    )
    def test_malformed_values_are_refused(self, value: str) -> None:
        """Every one a typo, and every one a refusal of *this* type — a bare
        ``ValueError`` would slip past the lifespan's except clause (no CRITICAL
        line naming the variable) and past the render path's guard."""
        with pytest.raises(PublicBaseUrlInvalid):
            public_base_url({PUBLIC_BASE_URL_ENV: value})

    def test_the_refusal_names_the_variable(self) -> None:
        with pytest.raises(PublicBaseUrlInvalid, match=PUBLIC_BASE_URL_ENV):
            public_base_url({PUBLIC_BASE_URL_ENV: "co-watcher.exe.xyz"})

    @pytest.mark.parametrize(
        "value", ["https://ops:s3cret@co-watcher.exe.xyz", "https://ops:s3cret@[co-watcher"]
    )
    def test_a_credential_is_refused_and_never_echoed(self, value: str) -> None:
        """Every link would publish it — into Slack, into email. And the refusal
        is logged CRITICAL to journald, so it must not quote the value either."""
        with pytest.raises(PublicBaseUrlInvalid) as refused:
            public_base_url({PUBLIC_BASE_URL_ENV: value})
        assert "s3cret" not in str(refused.value)


class TestAssertPublicBaseUrl:
    """The startup check. A malformed value is a typo and refuses the start;
    an absent one degrades notifications, so it is said out loud rather than
    refused."""

    def test_malformed_refuses(self) -> None:
        with pytest.raises(PublicBaseUrlInvalid):
            assert_public_base_url({PUBLIC_BASE_URL_ENV: "co-watcher.exe.xyz"})

    def test_unset_with_notifications_enabled_warns(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="src.core.public_base_url"):
            assert_public_base_url({"WATCHER_NOTIFIER_ENABLED": "1"})
        assert any(PUBLIC_BASE_URL_ENV in r.getMessage() for r in caplog.records)

    def test_unset_without_notifications_is_quiet(self, caplog) -> None:
        """No notifier, no links to build: silence is correct, not an omission."""
        with caplog.at_level(logging.WARNING, logger="src.core.public_base_url"):
            assert_public_base_url({})
        assert caplog.records == []

    def test_a_valid_base_is_quiet(self, caplog) -> None:
        env = {PUBLIC_BASE_URL_ENV: "https://co-watcher.exe.xyz", "WATCHER_NOTIFIER_ENABLED": "1"}
        with caplog.at_level(logging.WARNING, logger="src.core.public_base_url"):
            assert_public_base_url(env)
        assert caplog.records == []
