"""Tests for ``scripts/check-css.sh``, the output.css gate (#352).

The gate runs in the pre-commit hook and the CI ``css`` job, both against a
tree where ``build-css.sh`` may never have run. ``vendor/*.layered.css`` is a
git-ignored build product, so its absence is a clean checkout, not staleness;
only a present-but-different one is stale. Each test copies ``src/dashboard``
and the scripts into a temp root and runs the real script there. Needs the
pinned Tailwind CLI, so it skips where ``tailwindcss`` is absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("tailwindcss") is None, reason="needs the Tailwind CLI (scripts/build-css.sh)"
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal repo root: the scripts and ``src/dashboard`` with one vendored sheet."""
    (tmp_path / "scripts").mkdir()
    for name in ("check-css.sh", "wrap-vendor-css.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, tmp_path / "scripts" / name)
    shutil.copytree(
        REPO_ROOT / "src" / "dashboard",
        tmp_path / "src" / "dashboard",
        ignore=shutil.ignore_patterns("__pycache__", "*.layered.css"),
    )
    vendor = tmp_path / "src" / "dashboard" / "static" / "css" / "vendor"
    vendor.mkdir(exist_ok=True)
    (vendor / "widget.min.css").write_text(".widget{color:red}")
    return tmp_path


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "check-css.sh")],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_checkout_passes(tree: Path):
    """No ``*.layered.css`` on disk (git-ignored, never built) is not a failure."""
    result = _check(tree)
    assert result.returncode == 0, result.stdout + result.stderr


def test_stale_layered_css_fails(tree: Path):
    """A layered file that no longer matches its ``*.min.css`` source is stale."""
    vendor = tree / "src" / "dashboard" / "static" / "css" / "vendor"
    (vendor / "widget.layered.css").write_text("@layer vendor {\n.widget{color:blue}\n}\n")
    result = _check(tree)
    assert result.returncode == 1
    assert "widget.layered.css is stale" in result.stdout


def test_stale_output_css_fails(tree: Path):
    """The main gate still holds: an edited output.css is stale."""
    output = tree / "src" / "dashboard" / "static" / "css" / "output.css"
    output.write_text(output.read_text() + ".hand-edit{color:red}")
    result = _check(tree)
    assert result.returncode == 1
    assert "output.css is stale" in result.stdout
