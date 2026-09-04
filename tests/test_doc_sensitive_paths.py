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
import sys
from pathlib import Path
from types import SimpleNamespace

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
    and the default is the *locale's* encoding. CPython hides that most of the
    time — under ``LC_ALL=C`` it turns on PEP 540 UTF-8 mode and the read
    succeeds — but the cover is gone under ``PYTHONUTF8=0`` (measured: every
    test here then died with ``UnicodeDecodeError`` instead of reporting on the
    list) and under any *named* non-UTF-8 locale, which enables no such mode.
    Naming the encoding costs nothing and removes the dependency; it is also
    what the rest of this suite does (tests/conftest.py, test_migration_chain).
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
        encoding="utf-8",
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

    def test_cross_tree_entries_are_documented(self, tracked_files: list[str]) -> None:
        """An entry reaching outside its own tree must say so in the header.

        Segment matching means `deploy/` also flags `tests/deploy/`. That is
        the intended trade, but only while the file says which entries make it:
        a reader who meets an undocumented hit reaches for the entry doing the
        real work. The note went incomplete twice by hand — once when it was
        written, once when `tools/` was added a commit later — so the check is
        mechanical rather than editorial (CR-11).
        """
        header = "\n".join(
            line
            for line in PATHS_FILE.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith("#")
        )
        undocumented = {}
        for entry in _parse_list_file(PATHS_FILE):
            outside = [
                f for f in tracked_files if _path_matches(f, entry) and not f.startswith(entry)
            ]
            if outside and entry not in header:
                undocumented[entry] = outside[:3]
        assert not undocumented, (
            f"these entries match files outside their own tree and are not "
            f"named in {PATHS_FILE.name}'s header: {undocumented}"
        )


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
    "untouched/file.txt",  # UNWATCHED_FILE
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
# The file the census runs are allowed to touch: it must match no entry below,
# or those runs take the hit path and never reach the census.
UNWATCHED_FILE = "untouched/file.txt"
LIVE_ENTRIES = ("AGENTS.md", "docs")
DEAD_ENTRIES = ("schema.sql", "src/models/")


def _fs_encodable(name: str) -> bool:
    """Can this filename be written to the filesystem as-is?

    The sandbox deliberately includes a non-ASCII path, and creating it needs
    the *filesystem* encoding, not the one every read here names explicitly.
    Under ``PYTHONUTF8=0`` in a C locale that encoding is ASCII and the write
    raises — erroring the whole differential section in exactly the environment
    CR-1 cited as the reason to name encodings. Drop the file there rather than
    the test: every other branch still runs, and the entry naming it simply
    matches nothing, which the gate tolerates while another entry is live.
    """
    try:
        name.encode(sys.getfilesystemencoding())
    except UnicodeEncodeError:
        return False
    return True


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _run_gate(repo: Path, entries: tuple[str, ...], base: str):
    """Run the real gate over `repo` with `entries` as its committed list."""
    (repo / ".skills").mkdir(exist_ok=True)
    (repo / ".skills" / "doc-sensitive-paths").write_text(
        "\n".join(entries) + "\n", encoding="utf-8"
    )
    return subprocess.run(
        ["bash", str(DOC_CHECK), "--base", base],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _bullets_after(stdout: str, prefix: str) -> list[str]:
    """Collect the ``  - `` bullets of the block introduced by `prefix`.

    Both blocks the gate prints — flagged files, and the entries that matched
    nothing — use the same bullet, and the hit path prints the advice list in a
    third. Anchoring on the introducing line and stopping at the blank one is
    what keeps the three from being read as one (CR-10).
    """
    lines = stdout.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)
    assert start is not None, f"no line starting {prefix!r} in:\n{stdout}"
    bullets = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        if line.startswith("  - "):
            bullets.append(line[4:])
    return bullets


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory):
    """A throwaway repo with two useful base refs.

    ``base_all`` predates every file, so a run against it changes all of them —
    the hit path. ``base_files`` predates only a touch of an unwatched file, so
    a run against it changes nothing the list can match — the path where the
    gate runs its dead-entry census, which is the verdict the mirror in this
    module is otherwise trusted on.
    """
    if shutil.which("git") is None:
        pytest.skip("git not available")
    if not DOC_CHECK.is_file():
        pytest.skip("vendored doc-check.sh not present")

    files = tuple(name for name in SANDBOX_FILES if _fs_encodable(name))

    repo = tmp_path_factory.mktemp("doc-check-sandbox")
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
    base_all = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    for name in files:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git([*ident, "commit", "-q", "-m", "files"], repo)
    base_files = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    (repo / UNWATCHED_FILE).write_text("y\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git([*ident, "commit", "-q", "-m", "touch an unwatched file"], repo)

    # Never committed, so the list itself is in no diff the gate examines.
    return SimpleNamespace(repo=repo, files=files, base_all=base_all, base_files=base_files)


def test_mirror_agrees_with_the_real_script(sandbox) -> None:
    """The hit path: same files flagged, file for file."""
    result = _run_gate(sandbox.repo, SANDBOX_ENTRIES, sandbox.base_all)
    assert result.returncode == 1, f"rc={result.returncode}\n{result.stderr}"
    assert result.stderr == "", result.stderr
    assert ".skills/doc-sensitive-paths" in result.stdout, result.stdout

    predicted = sorted(
        name
        for name in sandbox.files
        if any(_path_matches(name, entry) for entry in SANDBOX_ENTRIES)
    )
    assert sorted(_bullets_after(result.stdout, "Sensitive paths changed")) == predicted

    # The sandbox is only evidence if it actually exercised both verdicts.
    assert UNWATCHED_FILE not in predicted
    assert "pyproject.toml.bak" not in predicted
    assert "packages/pkg/pyproject.toml" in predicted
    assert "abc/x.py" not in predicted, "entry 'a*c/' must not glob"


def test_mirror_agrees_on_which_entries_are_dead(sandbox) -> None:
    """The census path — the verdict `test_every_entry_matches_a_tracked_file`
    makes about this repo, made here by the gate itself against a known tree.

    Nothing else compares the two: the hit path never consults the census, so a
    mirror that disagreed with it about a dead entry would be believed (CR-10).
    """
    entries = (*LIVE_ENTRIES, *DEAD_ENTRIES)
    result = _run_gate(sandbox.repo, entries, sandbox.base_files)
    assert result.returncode == 0, f"rc={result.returncode}\n{result.stderr}"
    assert result.stderr == "", result.stderr

    predicted_dead = sorted(
        entry for entry in entries if not any(_path_matches(f, entry) for f in sandbox.files)
    )
    assert predicted_dead == sorted(DEAD_ENTRIES), "fixture no longer tests what it says"
    assert sorted(_bullets_after(result.stdout, "Note:")) == predicted_dead


def test_an_all_dead_list_is_exit_2(sandbox) -> None:
    """A list that cannot hit anything is a gate that did not run.

    This module's own guard rests on that contract: it fails a *single* dead
    entry precisely because upstream only fails when every one of them is dead.
    """
    result = _run_gate(sandbox.repo, DEAD_ENTRIES, sandbox.base_files)
    assert result.returncode == 2, f"rc={result.returncode}\n{result.stdout}"
    assert "matches any" in result.stderr, result.stderr
