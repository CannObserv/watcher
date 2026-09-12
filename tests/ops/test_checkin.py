"""Tests for the backup job's dead-man check-in (#296 D9).

A backup that fails loudly still says nothing when it stops running — a
disabled timer, a dead VM and a wedged interpreter all produce zero failures
and zero traffic. So the job reports every run, success or not, to a notifier
monitor that alarms when a report fails to arrive (broker#3, notifier#56).
"""

import logging

import httpx
import pytest

from src.ops import checkin

MONITOR = "01M24A8CA2GT0M7WE57NEMD0EW"
#: RFC 2606's reserved TLD: never resolves, so never stale, and it keeps this
#: file clean under tests/test_notifier_isolation.py's sweep (#280).
BASE = "http://notifier.invalid:9000"
CONFIGURED = {
    checkin.BASE_URL_ENV: BASE,
    checkin.MONITOR_ID_ENV: MONITOR,
    checkin.API_KEY_ENV: "nk_backup",
}


class _Recorder:
    """A POST seam that records calls and answers from a script."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, payload: dict, headers: dict, timeout: float) -> int:
        self.calls.append((url, payload, headers))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestPostCheckin:
    def test_posts_to_the_configured_monitor_with_the_key(self) -> None:
        post = _Recorder(200)
        assert checkin.post_checkin("ok", {"outcome": "uploaded"}, environ=CONFIGURED, post=post)
        ((url, payload, headers),) = post.calls
        assert url == f"{BASE}/api/v1/monitors/{MONITOR}/checkin"
        assert payload == {"status": "ok", "variables": {"outcome": "uploaded"}}
        assert headers["X-API-Key"] == "nk_backup"

    def test_a_trailing_slash_on_the_base_is_not_doubled(self) -> None:
        post = _Recorder(200)
        checkin.post_checkin(
            "ok", {}, environ={**CONFIGURED, checkin.BASE_URL_ENV: BASE + "/"}, post=post
        )
        assert post.calls[0][0] == f"{BASE}/api/v1/monitors/{MONITOR}/checkin"

    def test_a_base_that_is_not_an_http_url_is_refused(self, caplog) -> None:
        """Configuration, not a constant (#280's rule for src/) — so it is
        validated rather than trusted."""
        post = _Recorder()
        environ = {**CONFIGURED, checkin.BASE_URL_ENV: "notifier.invalid:9000"}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []

    def test_unconfigured_warns_and_posts_nothing(self, caplog) -> None:
        post = _Recorder()
        with caplog.at_level(logging.WARNING, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ={}, post=post) is False
        assert post.calls == []
        assert any(checkin.MONITOR_ID_ENV in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "missing", [checkin.BASE_URL_ENV, checkin.MONITOR_ID_ENV, checkin.API_KEY_ENV]
    )
    def test_half_configured_is_an_error_and_posts_nothing(self, caplog, missing) -> None:
        """All three or none. A base and a key without an id — or an id without
        its key — cannot check in, and must say so rather than look wired."""
        post = _Recorder()
        environ = {name: value for name, value in CONFIGURED.items() if name != missing}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            checkin.post_checkin("ok", {}, environ=environ, post=post)
        assert post.calls == []
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_a_monitor_id_that_is_not_a_bare_id_is_refused(self, caplog) -> None:
        """It becomes a URL path segment."""
        post = _Recorder()
        environ = {**CONFIGURED, checkin.MONITOR_ID_ENV: "../tenants/x"}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []

    def test_a_server_error_is_retried_once(self) -> None:
        """Retry-safe by contract: a replayed check-in overwrites the first, and
        a repeated ``alert`` re-renders the same report (notifier#56)."""
        post = _Recorder(503, 200)
        assert checkin.post_checkin("alert", {}, environ=CONFIGURED, post=post) is True
        assert len(post.calls) == 2

    def test_a_transport_error_is_retried_once(self) -> None:
        post = _Recorder(httpx.ConnectError("no route"), 200)
        assert checkin.post_checkin("ok", {}, environ=CONFIGURED, post=post) is True
        assert len(post.calls) == 2

    def test_a_rejection_is_not_retried_and_never_raises(self, caplog) -> None:
        """A 404 is the wrong id (a tenant id is the same ULID shape), a 401 the
        wrong key: repeating either changes nothing."""
        post = _Recorder(404)
        with caplog.at_level(logging.WARNING, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=CONFIGURED, post=post) is False
        assert len(post.calls) == 1
        assert any("404" in r.getMessage() for r in caplog.records)

    def test_persistent_failure_gives_up_quietly(self, caplog) -> None:
        post = _Recorder(httpx.ConnectError("down"), httpx.ConnectError("down"))
        with caplog.at_level(logging.WARNING, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=CONFIGURED, post=post) is False
        assert len(post.calls) == 2

    @pytest.mark.parametrize(
        "base", ["http://[notifier.invalid:9000", "http://notifier.invalid:9o00"]
    )
    def test_a_base_that_cannot_be_parsed_is_refused_not_raised(self, caplog, base) -> None:
        """``urlsplit`` raises on a bad bracket and ``.port`` on a bad port —
        a typo in the env file, which must read as one, not as a traceback."""
        post = _Recorder()
        environ = {**CONFIGURED, checkin.BASE_URL_ENV: base}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []
        assert any(checkin.BASE_URL_ENV in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "error",
        [
            UnicodeEncodeError("ascii", "nk_bäckup", 4, 5, "ordinal not in range(128)"),
            httpx.InvalidURL("bad"),
            RuntimeError("anything else"),
        ],
    )
    def test_an_error_that_is_not_transport_is_contained(self, caplog, error) -> None:
        """Only a transport error is worth the retry, but nothing may escape: a
        non-ASCII key fails header encoding, an odd URL fails httpx's parser,
        and a check-in that raises would fail the backup it reports on."""
        post = _Recorder(error)
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=CONFIGURED, post=post) is False
        assert len(post.calls) == 1
        assert any(type(error).__name__ in r.getMessage() for r in caplog.records)
        assert all("nk_b" not in r.getMessage() for r in caplog.records)
