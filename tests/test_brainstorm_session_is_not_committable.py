"""The brainstorming visual companion's session files must be unstageable (#396).

Run with ``--project-dir`` — as both upstream's guide and ``docs/SKILLS.md``
direct — ``start-server.sh`` writes its session under
``<project>/.superpowers/brainstorm/``: ``.last-token`` (the session key that
gates the ``0.0.0.0`` bind), ``server-info`` and ``server.log`` (which embed
it), and the mockups. Here the project dir is the repo root, and the repo is
public. Upstream's guide only *reminds* the agent to ignore the directory.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists() or shutil.which("git") is None,
    reason="not a git checkout, or git is unavailable",
)

SESSION_FILES = [
    ".superpowers/brainstorm/.last-token",
    ".superpowers/brainstorm/.last-port",
    ".superpowers/brainstorm/12345-1706000000/state/server-info",
    ".superpowers/brainstorm/12345-1706000000/state/server.log",
    ".superpowers/brainstorm/12345-1706000000/content/layout.html",
]


@pytest.mark.parametrize("path", SESSION_FILES)
def test_session_file_is_ignored(path: str) -> None:
    """``git check-ignore`` exits 0 only for a path an ignore rule covers."""
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"{path} is committable; add .superpowers/ to .gitignore"
