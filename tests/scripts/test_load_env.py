"""Tests for ``scripts/load-env.sh``.

Every doc in this repo used to tell operators and agents to load secrets with::

    export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)

That one-liner has three defects, catalogued upstream in
gregoryfoster/skills#144 and reproduced here as regression tests:

* With **both files absent** the command substitution is empty, so it degrades
  to a bare ``export`` — which prints *every exported variable*, secrets
  included, into whatever transcript is capturing the shell.
* A ``#`` comment line reaches ``export`` as ``'#': not a valid identifier``,
  which under ``set -e`` kills the caller **before** the command it was meant
  to set up ever runs.
* ``xargs`` word-splits ``PW=two words`` into a wrong value and still exits 0.

``scripts/load-env.sh`` replaces it. It is *sourced*, not executed, so the
exports land in the caller's shell, and it parses each line rather than
sourcing the file — a secrets file is data, and must never be executed.

``WATCHER_SYSTEM_ENV_FILE`` / ``WATCHER_PROJECT_ENV_FILE`` override the two
default paths so these tests never touch the real ``/etc/watcher/.env``.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "load-env.sh"


def source_and_probe(
    system_env: Path | None,
    project_env: Path | None,
    probe: str,
    strict: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Source the script in a hermetic shell, then run ``probe``.

    ``strict`` mirrors how the ship-gate wrapper calls it (``set -euo
    pipefail``), so a script that trips ``set -u`` or dies on a malformed line
    fails the test rather than silently degrading.
    """
    prelude = "set -euo pipefail\n" if strict else ""
    script = f"{prelude}source '{SCRIPT}'\n{probe}\n"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "SECRET_FROM_PARENT": "must-not-be-printed",
    }
    if system_env is not None:
        env["WATCHER_SYSTEM_ENV_FILE"] = str(system_env)
    if project_env is not None:
        env["WATCHER_PROJECT_ENV_FILE"] = str(project_env)
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


class TestValueParsing:
    """Values survive the shapes ``xargs`` word-splitting used to corrupt."""

    def test_simple_value_is_exported(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("KEY_SIMPLE=plain\n")
        r = source_and_probe(f, None, 'echo "[$KEY_SIMPLE]"')
        assert r.returncode == 0, r.stderr
        assert "[plain]" in r.stdout

    def test_value_containing_spaces_is_not_split(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("PW=two words\n")
        r = source_and_probe(f, None, 'echo "[$PW]"')
        assert "[two words]" in r.stdout

    def test_matched_quotes_are_stripped(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("DQ=\"has spaces\"\nSQ='single'\n")
        r = source_and_probe(f, None, 'echo "[$DQ][$SQ]"')
        assert "[has spaces][single]" in r.stdout

    def test_value_containing_equals_is_preserved(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("DSN=postgresql://u:p@h/db?opt=1\n")
        r = source_and_probe(f, None, 'echo "[$DSN]"')
        assert "[postgresql://u:p@h/db?opt=1]" in r.stdout

    def test_glob_value_is_not_expanded(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("GLOBBY=*.py\n")
        r = source_and_probe(f, None, 'echo "[$GLOBBY]"')
        assert "[*.py]" in r.stdout

    def test_export_prefix_is_tolerated(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("export EXPORTED=viaexport\n")
        r = source_and_probe(f, None, 'echo "[$EXPORTED]"')
        assert "[viaexport]" in r.stdout

    def test_value_is_exported_to_child_processes(self, tmp_path: Path) -> None:
        """Sourcing must export, not merely assign — the gate runs children."""
        f = tmp_path / "sys.env"
        f.write_text("CHILD_VISIBLE=yes\n")
        r = source_and_probe(f, None, "bash -c 'echo \"[$CHILD_VISIBLE]\"'")
        assert "[yes]" in r.stdout


class TestMalformedInputIsNotFatal:
    """A bad line in a secrets file must not decide whether the caller runs."""

    def test_comment_line_does_not_kill_the_caller(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("# a comment\nKEY=value\n")
        r = source_and_probe(f, None, 'echo "[$KEY]"')
        assert r.returncode == 0, r.stderr
        assert "[value]" in r.stdout

    def test_blank_and_whitespace_lines_are_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("\n   \nKEY=value\n")
        r = source_and_probe(f, None, 'echo "[$KEY]"')
        assert r.returncode == 0, r.stderr
        assert "[value]" in r.stdout

    def test_malformed_key_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("BAD-KEY=skipped\nGOOD=kept\n")
        r = source_and_probe(f, None, 'echo "[$GOOD]"')
        assert r.returncode == 0, r.stderr
        assert "[kept]" in r.stdout

    def test_line_without_equals_is_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("notakeyline\nGOOD=kept\n")
        r = source_and_probe(f, None, 'echo "[$GOOD]"')
        assert r.returncode == 0, r.stderr
        assert "[kept]" in r.stdout

    def test_final_line_without_newline_is_read(self, tmp_path: Path) -> None:
        f = tmp_path / "sys.env"
        f.write_text("KEY=value")  # no trailing newline
        r = source_and_probe(f, None, 'echo "[$KEY]"')
        assert "[value]" in r.stdout

    def test_secrets_file_is_never_executed(self, tmp_path: Path) -> None:
        """Parsed as data: a command substitution stays literal text."""
        f = tmp_path / "sys.env"
        f.write_text("EVIL=$(touch /tmp/watcher-load-env-pwned)\n")
        sentinel = Path("/tmp/watcher-load-env-pwned")
        sentinel.unlink(missing_ok=True)
        r = source_and_probe(f, None, 'echo "[$EVIL]"')
        assert r.returncode == 0, r.stderr
        assert not sentinel.exists(), "load-env.sh executed the secrets file"


class TestMissingFiles:
    """The bare-``export`` secrets-dump defect must not reappear."""

    def test_both_files_absent_is_not_an_error(self, tmp_path: Path) -> None:
        r = source_and_probe(tmp_path / "nope.env", tmp_path / "also-nope.env", "echo done")
        assert r.returncode == 0, r.stderr
        assert "done" in r.stdout

    def test_both_files_absent_does_not_dump_the_environment(self, tmp_path: Path) -> None:
        r = source_and_probe(tmp_path / "nope.env", tmp_path / "also-nope.env", "echo done")
        assert "SECRET_FROM_PARENT" not in r.stdout
        assert "SECRET_FROM_PARENT" not in r.stderr


class TestPrecedence:
    """AGENTS.md: two files load in order, later overrides earlier."""

    def test_project_file_overrides_system_file(self, tmp_path: Path) -> None:
        sys_f = tmp_path / "sys.env"
        proj_f = tmp_path / "proj.env"
        sys_f.write_text("SHARED=from-system\nONLY_SYSTEM=sys\n")
        proj_f.write_text("SHARED=from-project\n")
        r = source_and_probe(sys_f, proj_f, 'echo "[$SHARED][$ONLY_SYSTEM]"')
        assert "[from-project][sys]" in r.stdout
