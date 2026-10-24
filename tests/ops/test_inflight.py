"""Tests for the patching-hosts ``inflight`` reader (#382).

The knob's ``inflight`` line runs this as ``exedev`` before every apply step and
inside the reboot chain, and the skill's gate passes only when stdout is the
single word ``0``. So stdout carries the count and nothing else, every failure
exits non-zero with stdout empty (the gate reads that as unknown, and refuses),
and the URL comes from the file named on the command line, never from argv.
"""

import psycopg
import pytest

from src.ops import inflight
from tests.conftest import TEST_DATABASE_URL

PRODUCTION_URL = "postgresql+asyncpg://watcher_app:s3cret@localhost:5432/watcher"
DEV_URL = "postgresql+asyncpg://watcher:pw@localhost:5432/watcher_dev"


@pytest.fixture
def env_file(tmp_path):
    def write(text: str):
        path = tmp_path / "watcher.env"
        path.write_text(text)
        return path

    return write


class TestReadEnvFile:
    """The rules ``scripts/load-env.sh`` parses by, so one file reads alike."""

    def test_comments_blanks_and_lines_without_equals_are_skipped(self, env_file) -> None:
        path = env_file("# a comment\n\n   \nNOT_AN_ASSIGNMENT\nA=1\n")
        assert inflight.read_env_file(path) == {"A": "1"}

    def test_export_prefix_and_surrounding_blanks_are_tolerated(self, env_file) -> None:
        path = env_file("  export A=1\nB =2\n")
        assert inflight.read_env_file(path) == {"A": "1", "B": "2"}

    def test_matched_quotes_are_stripped_and_the_value_keeps_its_equals(self, env_file) -> None:
        path = env_file('A="x=y"\nB=\'z\'\nC="unmatched\n')
        assert inflight.read_env_file(path) == {"A": "x=y", "B": "z", "C": '"unmatched'}

    def test_a_key_that_is_not_a_name_is_skipped(self, env_file) -> None:
        path = env_file("BAD-KEY=1\n=2\nGOOD=3\n")
        assert inflight.read_env_file(path) == {"GOOD": "3"}

    def test_a_last_line_without_a_newline_is_read(self, env_file) -> None:
        assert inflight.read_env_file(env_file("A=1")) == {"A": "1"}


class TestMain:
    def test_prints_the_count_alone(self, monkeypatch, env_file, capsys) -> None:
        seen = []
        monkeypatch.setattr(inflight, "count_doing", lambda conninfo: seen.append(conninfo) or 3)
        path = env_file(f"DATABASE_URL={DEV_URL}\n")
        assert inflight.main(["--env-file", str(path)], environ={}) == 0
        out, err = capsys.readouterr()
        assert out == "3\n"
        assert err == ""
        # The worker's own DSN rule (get_conninfo), not a copy of it.
        assert seen == ["postgresql://watcher:pw@localhost:5432/watcher_dev"]

    def test_the_file_decides_the_database_not_the_ambient_environment(
        self, monkeypatch, env_file
    ) -> None:
        """A shell with load-env.sh sourced, or a stray PROCRASTINATE_DATABASE_URL,
        must not redirect the gate away from the file it was handed."""
        seen = []
        monkeypatch.setattr(inflight, "count_doing", lambda conninfo: seen.append(conninfo) or 0)
        path = env_file(f"DATABASE_URL={DEV_URL}\n")
        ambient = {
            "DATABASE_URL": "postgresql://other@localhost/other_test",
            "PROCRASTINATE_DATABASE_URL": "postgresql://other@localhost/queue_test",
        }
        assert inflight.main(["--env-file", str(path)], environ=ambient) == 0
        assert seen == ["postgresql://watcher:pw@localhost:5432/watcher_dev"]

    def test_refuses_production_without_the_opt_in(self, monkeypatch, env_file, capsys) -> None:
        monkeypatch.setattr(inflight, "count_doing", lambda conninfo: pytest.fail("connected"))
        path = env_file(f"DATABASE_URL={PRODUCTION_URL}\n")
        assert inflight.main(["--env-file", str(path)], environ={}) == 2
        out, err = capsys.readouterr()
        assert out == ""
        assert "WATCHER_ALLOW_PRODUCTION_DB" in err

    def test_reads_production_with_the_opt_in(self, monkeypatch, env_file, capsys) -> None:
        monkeypatch.setattr(inflight, "count_doing", lambda conninfo: 0)
        path = env_file(f"DATABASE_URL={PRODUCTION_URL}\n")
        environ = {"WATCHER_ALLOW_PRODUCTION_DB": "1"}
        assert inflight.main(["--env-file", str(path)], environ=environ) == 0
        assert capsys.readouterr().out == "0\n"

    def test_an_unreadable_env_file_exits_2_with_nothing_on_stdout(self, tmp_path, capsys) -> None:
        assert inflight.main(["--env-file", str(tmp_path / "absent")], environ={}) == 2
        out, err = capsys.readouterr()
        assert out == ""
        assert "absent" in err

    def test_a_file_without_a_database_url_exits_2(self, env_file, capsys) -> None:
        assert inflight.main(["--env-file", str(env_file("A=1\n"))], environ={}) == 2
        out, err = capsys.readouterr()
        assert out == ""
        assert "DATABASE_URL" in err

    def test_a_database_error_exits_1_and_never_prints_the_url(
        self, monkeypatch, env_file, capsys
    ) -> None:
        def fail(conninfo: str) -> int:
            raise psycopg.OperationalError(f"connection failed: {conninfo}")

        monkeypatch.setattr(inflight, "count_doing", fail)
        path = env_file(f"DATABASE_URL={PRODUCTION_URL}\n")
        environ = {"WATCHER_ALLOW_PRODUCTION_DB": "1"}
        assert inflight.main(["--env-file", str(path)], environ=environ) == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert "OperationalError" in err
        assert "s3cret" not in err

    def test_env_file_is_required(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            inflight.main([], environ={})
        assert exc.value.code == 2
        assert capsys.readouterr().out == ""


@pytest.mark.integration
async def test_counts_doing_jobs_on_a_read_only_session(_procrastinate_schema) -> None:
    """Procrastinate's own table, and nothing else: one ``doing`` row moves the
    count by one, ``todo`` does not, and the session refuses a write.

    Counted as a delta: a dev server defaults to ``watcher_test`` too
    (``scripts/dev_server.sh``), and its worker may hold a job of its own."""
    conninfo = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    before = inflight.count_doing(conninfo)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        ids = [
            conn.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args) "
                "VALUES ('inflight_test', 'inflight_test', %s, '{}') RETURNING id",
                (status,),
            ).fetchone()[0]
            for status in ("doing", "todo")
        ]
        try:
            assert inflight.count_doing(conninfo) == before + 1
        finally:
            conn.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", (ids,))

    with inflight.connect_read_only(conninfo) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM procrastinate_jobs WHERE false")
