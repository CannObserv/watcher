"""Tests for the backup job's dead-man check-in (#296 D9).

A backup that fails loudly still says nothing when it stops running — a
disabled timer, a dead VM and a wedged interpreter all produce zero failures
and zero traffic. So the job reports every run, success or not, to a notifier
monitor that alarms when a report fails to arrive (broker#3, notifier#56).

The key is a systemd credential, never an environment variable (#297): the
unit's ``LoadCredential=`` hands the run a private copy under
``$CREDENTIALS_DIRECTORY``. Here a ``tmp_path`` directory plays that part.
"""

import logging
from pathlib import Path

import httpx
import pytest

from src.ops import checkin

MONITOR = "01M24A8CA2GT0M7WE57NEMD0EW"
#: RFC 2606's reserved TLD: never resolves, so never stale, and it keeps this
#: file clean under tests/test_notifier_isolation.py's sweep (#280).
BASE = "http://notifier.invalid:9000"
KEY = "nk_backup"


def _credentials(directory: Path, key: str | None = KEY) -> dict[str, str]:
    """A credentials directory holding ``key`` (no file at all when None)."""
    directory.mkdir(exist_ok=True)
    if key is not None:
        (directory / checkin.KEY_CREDENTIAL).write_text(key)
    return {checkin.CREDENTIALS_DIRECTORY_ENV: str(directory)}


@pytest.fixture
def configured(tmp_path) -> dict[str, str]:
    """All three: the base and monitor id from the environment, the key from
    its credential."""
    return {
        checkin.BASE_URL_ENV: BASE,
        checkin.MONITOR_ID_ENV: MONITOR,
        **_credentials(tmp_path / "credentials"),
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
    def test_posts_to_the_configured_monitor_with_the_key(self, configured) -> None:
        post = _Recorder(200)
        assert checkin.post_checkin("ok", {"outcome": "uploaded"}, environ=configured, post=post)
        ((url, payload, headers),) = post.calls
        assert url == f"{BASE}/api/v1/monitors/{MONITOR}/checkin"
        assert payload == {"status": "ok", "variables": {"outcome": "uploaded"}}
        assert headers["X-API-Key"] == KEY

    def test_a_trailing_slash_on_the_base_is_not_doubled(self, configured) -> None:
        post = _Recorder(200)
        checkin.post_checkin(
            "ok", {}, environ={**configured, checkin.BASE_URL_ENV: BASE + "/"}, post=post
        )
        assert post.calls[0][0] == f"{BASE}/api/v1/monitors/{MONITOR}/checkin"

    def test_a_base_that_is_not_an_http_url_is_refused(self, configured, caplog) -> None:
        """Configuration, not a constant (#280's rule for src/) — so it is
        validated rather than trusted."""
        post = _Recorder()
        environ = {**configured, checkin.BASE_URL_ENV: "notifier.invalid:9000"}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []

    def test_unconfigured_warns_and_posts_nothing(self, caplog) -> None:
        post = _Recorder()
        with caplog.at_level(logging.WARNING, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ={}, post=post) is False
        assert post.calls == []
        assert any(checkin.MONITOR_ID_ENV in r.getMessage() for r in caplog.records)

    def test_unconfigured_under_the_unit_warns_and_posts_nothing(self, tmp_path, caplog) -> None:
        """What the unit hands the job until the monitor exists: its key file
        must exist, so it is empty, and neither variable is set."""
        post = _Recorder()
        environ = _credentials(tmp_path / "credentials", key="")
        with caplog.at_level(logging.WARNING, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    @pytest.mark.parametrize("missing", [checkin.BASE_URL_ENV, checkin.MONITOR_ID_ENV])
    def test_half_configured_is_an_error_and_posts_nothing(
        self, configured, caplog, missing
    ) -> None:
        """All three or none. A base and a key without an id — or an id without
        its key — cannot check in, and must say so rather than look wired."""
        post = _Recorder()
        environ = {name: value for name, value in configured.items() if name != missing}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            checkin.post_checkin("ok", {}, environ=environ, post=post)
        assert post.calls == []
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.parametrize("key", ["", None], ids=["empty", "absent"])
    def test_a_key_credential_empty_or_absent_is_half_configured(
        self, configured, tmp_path, caplog, key
    ) -> None:
        """The base and id set, the key file left empty — the monitor half
        provisioned — is the same error as any other missing third."""
        post = _Recorder()
        environ = {**configured, **_credentials(tmp_path / "other", key=key)}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []
        assert any(checkin.KEY_CREDENTIAL in r.getMessage() for r in caplog.records)

    def test_a_monitor_id_that_is_not_a_bare_id_is_refused(self, configured, caplog) -> None:
        """It becomes a URL path segment."""
        post = _Recorder()
        environ = {**configured, checkin.MONITOR_ID_ENV: "../tenants/x"}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []

    def test_a_server_error_is_retried_once(self, configured) -> None:
        """Retry-safe by contract: a replayed check-in overwrites the first, and
        a repeated ``alert`` re-renders the same report (notifier#56)."""
        post = _Recorder(503, 200)
        assert checkin.post_checkin("alert", {}, environ=configured, post=post) is True
        assert len(post.calls) == 2

    def test_a_transport_error_is_retried_once(self, configured) -> None:
        post = _Recorder(httpx.ConnectError("no route"), 200)
        assert checkin.post_checkin("ok", {}, environ=configured, post=post) is True
        assert len(post.calls) == 2

    def test_a_rejection_is_not_retried_and_never_raises(self, configured, caplog) -> None:
        """A 404 is the wrong id (a tenant id is the same ULID shape), a 401 the
        wrong key: repeating either changes nothing."""
        post = _Recorder(404)
        with caplog.at_level(logging.WARNING, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=configured, post=post) is False
        assert len(post.calls) == 1
        assert any("404" in r.getMessage() for r in caplog.records)

    def test_persistent_failure_gives_up_quietly(self, configured, caplog) -> None:
        post = _Recorder(httpx.ConnectError("down"), httpx.ConnectError("down"))
        with caplog.at_level(logging.WARNING, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=configured, post=post) is False
        assert len(post.calls) == 2

    @pytest.mark.parametrize(
        "base", ["http://[notifier.invalid:9000", "http://notifier.invalid:9o00"]
    )
    def test_a_base_that_cannot_be_parsed_is_refused_not_raised(
        self, configured, caplog, base
    ) -> None:
        """``urlsplit`` raises on a bad bracket and ``.port`` on a bad port —
        a typo in the env file, which must read as one, not as a traceback."""
        post = _Recorder()
        environ = {**configured, checkin.BASE_URL_ENV: base}
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
    def test_an_error_that_is_not_transport_is_contained(self, configured, caplog, error) -> None:
        """Only a transport error is worth the retry, but nothing may escape: a
        non-ASCII key fails header encoding, an odd URL fails httpx's parser,
        and a check-in that raises would fail the backup it reports on."""
        post = _Recorder(error)
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=configured, post=post) is False
        assert len(post.calls) == 1
        assert any(type(error).__name__ in r.getMessage() for r in caplog.records)
        assert all("nk_b" not in r.getMessage() for r in caplog.records)


class TestKeyCredential:
    """The key is read from ``$CREDENTIALS_DIRECTORY`` and nowhere else (#297)."""

    def test_the_key_is_never_taken_from_the_environment(self, caplog) -> None:
        """The whole point of the credential: a key in the environment is in
        every child's, and in ``/proc/<pid>/environ``. The variable it once was
        is ignored, so a stale env file cannot quietly bring it back."""
        post = _Recorder(200)
        environ = {
            checkin.BASE_URL_ENV: BASE,
            checkin.MONITOR_ID_ENV: MONITOR,
            "WATCHER_BACKUP_NOTIFIER_API_KEY": KEY,
        }
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []

    def test_the_trailing_newline_of_a_key_file_is_not_part_of_the_key(
        self, configured, tmp_path
    ) -> None:
        post = _Recorder(200)
        environ = {**configured, **_credentials(tmp_path / "nl", key=f"{KEY}\n")}
        assert checkin.post_checkin("ok", {}, environ=environ, post=post) is True
        assert post.calls[0][2]["X-API-Key"] == KEY

    def test_a_key_that_cannot_be_read_is_contained(self, configured, tmp_path, caplog) -> None:
        """A credential that is a directory, or unreadable, is a check-in that
        did not land — never a traceback that fails the backup."""
        directory = tmp_path / "odd"
        (directory / checkin.KEY_CREDENTIAL).mkdir(parents=True)
        post = _Recorder(200)
        environ = {**configured, checkin.CREDENTIALS_DIRECTORY_ENV: str(directory)}
        with caplog.at_level(logging.ERROR, logger="src.ops.checkin"):
            assert checkin.post_checkin("ok", {}, environ=environ, post=post) is False
        assert post.calls == []
        assert any("IsADirectoryError" in r.getMessage() for r in caplog.records)
