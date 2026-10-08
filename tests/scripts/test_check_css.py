"""Tests for ``scripts/check-css.sh``, the output.css gate (#352).

The gate runs in the pre-commit hook and the CI ``css`` job, both against a
tree where ``build-css.sh`` may never have run. ``vendor/*.layered.css`` is a
git-ignored build product, so its absence is a clean checkout, not staleness;
only a present-but-different one is stale. Each test copies ``src/dashboard``
and the scripts into a temp root and runs the real script there.

The real-build tests need the pinned Tailwind CLI and skip without it, so **CI
never runs them**: the ``test`` job has no CLI, and the ``css`` job runs
``check-css.sh`` itself rather than pytest. They run on co-watcher. The
fake-CLI tests stand a stub on ``PATH``, need only bash and npm, and do run in
the ``test`` job.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

needs_cli = pytest.mark.skipif(
    shutil.which("tailwindcss") is None, reason="needs the Tailwind CLI (scripts/build-css.sh)"
)
# The pin, read from the script under test so a bump needs no edit here.
(PINNED,) = re.findall(
    r'^TAILWIND_CLI_VERSION="([^"]+)"$',
    (REPO_ROOT / "scripts" / "check-css.sh").read_text(encoding="utf-8"),
    re.MULTILINE,
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


def _check(root: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "check-css.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# The banner as the CLI prints it when ``CI`` is set (GitHub Actions): colour
# escapes between "tailwindcss " and the version, even off a TTY.
_ANSI_BANNER = (
    "\\033[3m\\033[1m\\033[34m≈\\033[39m\\033[22m\\033[23m tailwindcss \\033[34mv{}\\033[39m"
)


def _fake_cli(
    tmp_path: Path, version: str, build: str, banner: str = "≈ tailwindcss v{}"
) -> dict[str, str]:
    """Env with a stub ``tailwindcss`` first on PATH: ``--help`` prints ``banner``
    filled with ``version``, anything else runs the shell snippet ``build``."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    cli = bin_dir / "tailwindcss"
    cli.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = --help ]; then printf '{banner.format(version)}\\n'; exit 0; fi\n"
        f"{build}\n"
    )
    cli.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


@needs_cli
def test_clean_checkout_passes(tree: Path):
    """No ``*.layered.css`` on disk (git-ignored, never built) is not a failure."""
    result = _check(tree)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_cli
def test_stale_layered_css_fails(tree: Path):
    """A layered file that no longer matches its ``*.min.css`` source is stale."""
    vendor = tree / "src" / "dashboard" / "static" / "css" / "vendor"
    (vendor / "widget.layered.css").write_text("@layer vendor {\n.widget{color:blue}\n}\n")
    result = _check(tree)
    assert result.returncode == 1
    assert "widget.layered.css is stale" in result.stdout


@needs_cli
def test_stale_output_css_fails(tree: Path):
    """The main gate still holds: an edited output.css is stale."""
    output = tree / "src" / "dashboard" / "static" / "css" / "output.css"
    output.write_text(output.read_text() + ".hand-edit{color:red}")
    result = _check(tree)
    assert result.returncode == 1
    assert "output.css is stale" in result.stdout


def test_build_failure_prints_its_error(tree: Path, tmp_path: Path):
    """A failed Tailwind build says so and shows the CLI's error, not a bare exit 1."""
    env = _fake_cli(tmp_path, PINNED, 'echo "Error: Can\'t resolve tailwindcss" >&2; exit 1')
    result = _check(tree, env)
    assert result.returncode == 1
    assert "tailwindcss build failed" in result.stdout
    assert "Can't resolve tailwindcss" in result.stdout + result.stderr


def test_unpinned_cli_is_named_not_called_stale(tree: Path, tmp_path: Path):
    """Another CLI version fails as a version mismatch before building: its build
    would differ, and "output.css is stale" would send the reader to rebuild with it."""
    built = tmp_path / "built"
    env = _fake_cli(tmp_path, "9.9.9", f"touch {built}; exit 0")
    result = _check(tree, env)
    assert result.returncode == 1
    assert f"tailwindcss v9.9.9 found, pinned v{PINNED}" in result.stdout
    assert "stale" not in result.stdout
    assert not built.exists()


def test_coloured_banner_still_reads_the_version(tree: Path, tmp_path: Path):
    """Under ``CI`` the banner carries colour escapes; the pinned version still passes
    the version check (the build then runs, and this stub fails it on purpose)."""
    env = _fake_cli(tmp_path, PINNED, "echo reached-build >&2; exit 1", banner=_ANSI_BANNER)
    result = _check(tree, env)
    assert "found, pinned" not in result.stdout
    assert "reached-build" in result.stdout


def test_coloured_banner_names_the_wrong_version(tree: Path, tmp_path: Path):
    """A coloured banner with another version is named, not read as "unknown"."""
    env = _fake_cli(tmp_path, "9.9.9", "exit 0", banner=_ANSI_BANNER)
    result = _check(tree, env)
    assert result.returncode == 1
    assert f"tailwindcss v9.9.9 found, pinned v{PINNED}" in result.stdout


def test_banner_after_a_warning_line_still_reads(tree: Path, tmp_path: Path):
    """Node can print a warning ahead of the banner (e.g. NO_COLOR with FORCE_COLOR set)."""
    banner = "(node:1) Warning: NO_COLOR is ignored\\n≈ tailwindcss v{}"
    env = _fake_cli(tmp_path, PINNED, "echo reached-build >&2; exit 1", banner=banner)
    result = _check(tree, env)
    assert "found, pinned" not in result.stdout
    assert "reached-build" in result.stdout


def test_missing_cli_hint_installs_the_pin(tree: Path, tmp_path: Path):
    """With no ``tailwindcss`` on PATH, the hint names the pinned install."""
    bin_dir = tmp_path / "bare"
    bin_dir.mkdir()
    (bin_dir / "dirname").symlink_to(
        shutil.which("dirname")
    )  # the script's only tool before the check
    result = subprocess.run(
        [shutil.which("bash"), str(tree / "scripts" / "check-css.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(bin_dir)},
    )
    assert result.returncode == 1
    assert f"npm install -g @tailwindcss/cli@{PINNED}" in result.stdout
