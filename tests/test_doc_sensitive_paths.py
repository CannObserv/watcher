"""The `.skills/` tailoring for `doc-check.sh` must stay live (#281).

`doc-check.sh` (vendored with `shipping-work-python-fastapi`) matches its
sensitive-path list against *path segments*, and a list whose entries match
nothing prints the same clean green as a genuinely doc-neutral branch — the
gregoryfoster/skills#252 defect. Upstream now exits 2 when **no** entry matches
any tracked file, but an entry that individually matches nothing is still
silent noise: it appears in the gate's "could not have contributed" note and
nowhere else.

This repo tailors the list in `.skills/doc-sensitive-paths`, so the dead-entry
question is now ours to answer. These tests answer it against the working tree:
every entry names something this repo actually tracks, and the advice file that
must be tailored alongside it (upstream's paired-tailoring rule) names docs
that exist.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PATHS_FILE = REPO_ROOT / ".skills" / "doc-sensitive-paths"
SECTIONS_FILE = REPO_ROOT / ".skills" / "doc-sections"
DOC_CHECK = REPO_ROOT / "skills" / "shipping-work-python-fastapi" / "scripts" / "doc-check.sh"


def _parse_list_file(path: Path) -> list[str]:
    """Parse a `.skills/` list file the way `doc-check.sh` does.

    One entry per line; blank lines and whole-line ``#`` comments dropped;
    surrounding whitespace trimmed. A ``#`` later in a line is content, not a
    comment — the advice lines cite issue numbers.

    ``encoding="utf-8"`` is not decoration (CR-1): both files carry em-dashes,
    and the default is the *locale's* encoding, so under ``LC_ALL=C`` — a
    systemd timer, a cron job, a runner with ``LANG`` unset — every test here
    died with ``UnicodeDecodeError`` instead of reporting on the list.
    """
    entries: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def _path_matches(file: str, entry: str) -> bool:
    """Mirror of `doc-check.sh`'s `path_matches` — segment matching.

    A trailing-slash entry matches a directory at any depth; a slash-less entry
    names a file or a directory, and every continuation requires a literal
    ``/`` so ``pyproject.toml`` does not also claim ``pyproject.toml.bak``.

    The entry is matched **literally**, and that is not an approximation of the
    shell: `doc-check.sh` writes its patterns as ``"$entry"*``, and a quoted
    expansion inside a `case` pattern makes its ``*``, ``?`` and ``[…]``
    ordinary characters. Only the unquoted ``*`` around it globs. Do not
    "fix" this into `fnmatch` — that would make ``a*c/`` match ``abc/`` here
    and nowhere else. `test_mirror_agrees_with_the_real_script` runs the real
    gate over exactly that case, so the question is settled by execution
    rather than by this docstring.
    """
    if entry.endswith("/"):
        return file.startswith(entry) or f"/{entry}" in file
    return (
        file == entry
        or file.endswith(f"/{entry}")
        or file.startswith(f"{entry}/")
        or f"/{entry}/" in file
    )


@pytest.fixture(scope="module")
def tracked_files() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


class TestSensitivePaths:
    def test_file_is_committed_and_non_empty(self) -> None:
        assert PATHS_FILE.is_file(), f"{PATHS_FILE} is missing"
        assert _parse_list_file(PATHS_FILE), "list parses to no entries — exit 2"

    def test_every_entry_matches_a_tracked_file(self, tracked_files: list[str]) -> None:
        """A dead entry documents a tree this repo does not have.

        Upstream only fails when *every* entry is dead. One dead entry still
        misleads: the list reads as coverage of something that is not there.
        """
        dead = [
            entry
            for entry in _parse_list_file(PATHS_FILE)
            if not any(_path_matches(f, entry) for f in tracked_files)
        ]
        assert not dead, f"entries match no tracked file: {dead}"


class TestDocSections:
    def test_file_is_committed_and_non_empty(self) -> None:
        """Upstream's paired-tailoring rule: tailor the advice with the list.

        A repo that tailors only the path list gets advice written for the
        default stack — sections it may not keep, and silence about the docs
        that actually drift (gregoryfoster/skills#261).
        """
        assert SECTIONS_FILE.is_file(), f"{SECTIONS_FILE} is missing"
        assert _parse_list_file(SECTIONS_FILE), "advice parses to no sections — exit 2"

    def test_every_section_names_a_tracked_doc(self, tracked_files: list[str]) -> None:
        """Advice pointing at a deleted doc sends the reader nowhere.

        Each line must read ``<tracked doc path>: <what to check>`` — the
        contract the file's own header states. Enforcing it here is what makes
        the advice auditable; the failure message says so, because a line
        written as plain prose otherwise fails as "untracked doc" and sends the
        reader looking for a file rather than for the format (CR-4).
        """
        missing = []
        for line in _parse_list_file(SECTIONS_FILE):
            doc = line.split(":", 1)[0].strip()
            if doc not in tracked_files:
                missing.append(doc)
        assert not missing, (
            f"{SECTIONS_FILE.name} lines must start '<tracked doc path>: ', "
            f"and these do not name a tracked file: {missing}"
        )


@pytest.mark.skipif(not DOC_CHECK.is_file(), reason="vendored doc-check.sh not present")
def test_doc_check_accepts_the_tailoring() -> None:
    """End-to-end: the gate reads both override files without exit 2.

    ``--base HEAD`` yields an empty diff, so this exercises exactly the
    override parsing that runs before any diff is examined — and the outcome is
    therefore deterministic: exit 0, nothing on stderr. Asserting only "not 2"
    would let exit 127 pass (no bash, or a symlink into an uninitialized
    submodule), which is the check silently not running — the failure shape
    gregoryfoster/skills#252 was about, one level up (CR-3).
    """
    result = subprocess.run(
        ["bash", str(DOC_CHECK), "--base", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"rc={result.returncode}\n{result.stderr}"
    assert result.stderr == "", result.stderr


# --- Differential check against the real gate ---------------------------------
#
# Everything above trusts `_path_matches` to be what `doc-check.sh` does. That
# trust is the whole risk: a mirror that has drifted vouches for a list the gate
# reads differently, and both then print green. So run the real script over a
# throwaway repo built to hit every branch of its matcher, and require the two
# to agree file for file.
SANDBOX_FILES = (
    "AGENTS.md",
    "deploy/watcher.service",
    "tests/deploy/test_unit.py",
    "pyproject.toml",
    "packages/pkg/pyproject.toml",
    "pyproject.toml.bak",
    "src/core/models/a.py",
    "src/api/routes.py",
    "docs/guide.md",
    "abc/x.py",
    "a*c/x.py",
    "src/café.py",
    "untouched/file.txt",
)
# Chosen for the branches, not for realism: a directory entry and a bare-name
# entry, a name whose ".bak" sibling must NOT match, a nested package copy that
# must, a non-ASCII path (git C-quotes it without core.quotePath=false, and the
# quote defeats the anchored half of the match), and `a*c/` — literal under the
# shell's quoting, glob under any `fnmatch` rewrite of the mirror.
SANDBOX_ENTRIES = (
    "AGENTS.md",
    "deploy/",
    "pyproject.toml",
    "src/core/",
    "docs",
    "a*c/",
    "src/café.py",
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _parse_hit_block(stdout: str) -> list[str]:
    """Take the flagged files — the block before the blank line.

    The advice that follows uses the same ``  - `` bullet, so a naive scan of
    every bullet would fold nine doc sections into the file list and compare
    green against nothing.
    """
    lines = stdout.splitlines()
    assert lines and lines[0].startswith("Sensitive paths changed"), stdout
    hits = []
    for line in lines[1:]:
        if not line.strip():
            break
        assert line.startswith("  - "), f"unexpected hit line: {line!r}"
        hits.append(line[4:])
    return hits


@pytest.mark.skipif(not DOC_CHECK.is_file(), reason="vendored doc-check.sh not present")
def test_mirror_agrees_with_the_real_script(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo = tmp_path / "sandbox"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    ident = [
        "-c",
        "user.email=cr@example.invalid",
        "-c",
        "user.name=CR",
        "-c",
        "commit.gpgsign=false",
    ]
    _git([*ident, "commit", "-q", "--allow-empty", "-m", "base"], repo)
    base = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    for name in SANDBOX_FILES:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git([*ident, "commit", "-q", "-m", "files"], repo)

    # Written after the commit so the list itself is not part of the diff.
    (repo / ".skills").mkdir()
    (repo / ".skills" / "doc-sensitive-paths").write_text(
        "\n".join(SANDBOX_ENTRIES) + "\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["bash", str(DOC_CHECK), "--base", base],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1, f"rc={result.returncode}\n{result.stderr}"
    assert ".skills/doc-sensitive-paths" in result.stdout, result.stdout

    predicted = sorted(
        name
        for name in SANDBOX_FILES
        if any(_path_matches(name, entry) for entry in SANDBOX_ENTRIES)
    )
    assert sorted(_parse_hit_block(result.stdout)) == predicted

    # The sandbox is only evidence if it actually exercised both verdicts.
    assert "untouched/file.txt" not in predicted
    assert "pyproject.toml.bak" not in predicted
    assert "packages/pkg/pyproject.toml" in predicted
    assert "abc/x.py" not in predicted, "entry 'a*c/' must not glob"
