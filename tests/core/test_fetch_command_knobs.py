"""Tests for the fetch-command env knobs (CR-1, CR-3).

Separate from ``test_fetch_commands.py`` because that module carries a
module-level ``integration`` mark: these are pure environment reads and belong
in the default suite.

Both knobs are read on paths that handle a failure. A knob that *raises* there
escapes the handler, so the row keeps whatever status it had — and the reaper
resurrects it forever. Same reasoning as ``validator_max_age`` (#269 CR-6): the
knob must not be able to wedge the path it governs.
"""

import logging

from src.core.fetch_commands import (
    DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_FETCH_MAX_REISSUES,
    FETCH_COMMAND_TIMEOUT_ENV,
    FETCH_MAX_REISSUES_ENV,
    fetch_command_timeout_seconds,
    fetch_max_reissues,
)


class TestFetchMaxReissues:
    def test_defaults_to_three(self, monkeypatch):
        monkeypatch.delenv(FETCH_MAX_REISSUES_ENV, raising=False)
        assert fetch_max_reissues() == DEFAULT_FETCH_MAX_REISSUES

    def test_reads_the_env_override(self, monkeypatch):
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "5")
        assert fetch_max_reissues() == 5

    def test_an_unparseable_value_falls_back_to_the_default(self, monkeypatch, caplog):
        # CR-1: this is read inside ``except BlobUnreadable`` in the blob apply.
        # Raising there escapes the handler, leaves the row IN_FLIGHT holding a
        # fact, and the reaper re-defers the same doomed apply every window —
        # the unbounded loop #275 removed, in env-var form.
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "three")
        with caplog.at_level(logging.WARNING, logger="src.core.fetch_commands"):
            assert fetch_max_reissues() == DEFAULT_FETCH_MAX_REISSUES
        assert any(FETCH_MAX_REISSUES_ENV in r.getMessage() for r in caplog.records)

    def test_zero_is_honoured_as_no_reissues(self, monkeypatch):
        # Safe direction — an unreadable blob fails on the first occasion — so
        # it passes through rather than being corrected to the default.
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "0")
        assert fetch_max_reissues() == 0


class TestFetchCommandTimeoutSeconds:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv(FETCH_COMMAND_TIMEOUT_ENV, raising=False)
        assert fetch_command_timeout_seconds() == DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS

    def test_reads_the_env_override(self, monkeypatch):
        monkeypatch.setenv(FETCH_COMMAND_TIMEOUT_ENV, "60")
        assert fetch_command_timeout_seconds() == 60.0

    def test_an_unparseable_value_falls_back_to_the_default(self, monkeypatch, caplog):
        # CR-3: worse blast radius than the cap — this one is read once per
        # reaper pass, so a typo takes out the whole sweep, not one item.
        monkeypatch.setenv(FETCH_COMMAND_TIMEOUT_ENV, "half an hour")
        with caplog.at_level(logging.WARNING, logger="src.core.fetch_commands"):
            assert fetch_command_timeout_seconds() == DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS
        assert any(FETCH_COMMAND_TIMEOUT_ENV in r.getMessage() for r in caplog.records)
