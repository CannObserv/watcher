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
    """
    entries: list[str] = []
    for raw in path.read_text().splitlines():
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
        """Advice pointing at a deleted doc sends the reader nowhere."""
        missing = []
        for line in _parse_list_file(SECTIONS_FILE):
            doc = line.split(":", 1)[0].strip()
            if doc not in tracked_files:
                missing.append(doc)
        assert not missing, f"advice names untracked docs: {missing}"


@pytest.mark.skipif(not DOC_CHECK.is_file(), reason="vendored doc-check.sh not present")
def test_doc_check_accepts_the_tailoring() -> None:
    """End-to-end: the gate reads both override files without exit 2.

    ``--base HEAD`` yields an empty diff, so this exercises exactly the
    override parsing that runs before any diff is examined. Exit 2 here means a
    committed tailoring is unusable — which upstream deliberately refuses to
    treat as "absent".
    """
    result = subprocess.run(
        ["bash", str(DOC_CHECK), "--base", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 2, result.stderr
