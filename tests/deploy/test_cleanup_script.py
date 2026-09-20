"""What `scripts/cleanup.sh` has to keep once the Docker branch is gone (#310).

The script runs under `set -euo pipefail`, so a non-zero exit anywhere in it
ends the run: every guard in it is load-bearing for every step *below* it, and
the weekly timer reports only the unit's exit status, so an abort reads as a run
that simply ended. That assertion arrived here from
`tests/deploy/test_cleanup_docker_guard.py`, which #300 wrote around the Docker
branch and #310 deleted with it — it was always about the script, not about
Docker.

The deleted file's premise is now pinned in reverse. #300 moved the semantic
index to the cohort's shared store on `co-index` and #310 purged the packages
from this host, so a `docker` call here would no longer be a branch that skips
itself on a daemon that is down — it would be `command not found`, aborting the
weekly run at that line and silently skipping the journal vacuum below it.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLEANUP = REPO_ROOT / "scripts" / "cleanup.sh"

#: Resolved once; absent only on a host that cannot run the script either.
BASH = shutil.which("bash")

#: Stands in for every step below the npm block in the real script.
SENTINEL = "CLEANUP_REACHED_THE_END"


def test_pipefail_is_what_makes_every_guard_load_bearing():
    """The premise of this whole file. If it ever goes, re-derive the rest."""
    assert "set -euo pipefail" in CLEANUP.read_text()


def test_the_script_never_calls_docker():
    """No binary on this host answers it, and `set -e` makes that fatal (#310).

    Checked over the whole file, comments included: a comment describing a
    branch that no longer exists is the thing #310 removed, not a survivor.
    """
    assert "docker" not in CLEANUP.read_text().lower(), (
        "scripts/cleanup.sh names Docker again — #310 purged the packages from "
        "this host, so the call is `command not found` and aborts the run"
    )


def test_the_journal_vacuum_still_runs():
    """The step the removed guard existed to protect — pinned to outlive it."""
    assert "--vacuum-time=14d" in CLEANUP.read_text()


def test_the_steps_the_branch_sat_between_both_survived_it():
    """A seam has two sides, and a line range can miss in either direction.

    The Docker branch sat between the APT cache clean and the journal vacuum, so
    a range that starts a few lines early takes the APT step instead — which the
    test above, pinning only the lower side, would not notice.
    """
    text = CLEANUP.read_text()
    assert "sudo /bin/apt-get clean" in text, "the step above the removed branch went with it"
    assert text.index("sudo /bin/apt-get clean") < text.index("--vacuum-time=14d")


@pytest.mark.skipif(BASH is None, reason="needs bash to parse the script")
def test_the_script_still_parses():
    """The one property above that no amount of reading the text establishes.

    #310 removed the Docker branch as a line range. Clip one line short and an
    orphan `fi` is left behind; one line long and an `if` loses its close. Both
    satisfy every substring assertion in this file, and both abort the weekly
    run at parse time — before the first step, not partway down it. `bash -n` is
    the cheapest thing that can tell the difference.
    """
    result = subprocess.run([BASH, "-n", str(CLEANUP)], capture_output=True, text=True)
    assert result.returncode == 0, f"scripts/cleanup.sh does not parse:\n{result.stderr}"


def _npm_block() -> str:
    """The npm cache step, with the threshold constant it reads.

    Extracted rather than reproduced: a test that hard-codes the threshold
    passes while the script carries a different one.
    """
    lines = CLEANUP.read_text().splitlines()
    threshold = next((ln for ln in lines if ln.startswith("NPM_CACHE_THRESHOLD_BYTES=")), None)
    if threshold is None:
        pytest.fail("scripts/cleanup.sh declares no NPM_CACHE_THRESHOLD_BYTES (#314)")
    start = next((i for i, ln in enumerate(lines) if "--- npm caches ---" in ln), None)
    if start is None:
        pytest.fail("scripts/cleanup.sh has no npm caches step")
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("# uv cache prune")),
        None,
    )
    if end is None:
        pytest.fail("the npm caches step has no following step to bound it")
    return "\n".join([threshold] + lines[start:end])


def _run_npm_block(home: Path) -> subprocess.CompletedProcess:
    """Run the extracted step under `set -euo pipefail` against a stub HOME.

    The real PATH is kept: this step calls only `du`, `cut`, `numfmt` and `rm`,
    and stubbing those would test the stubs rather than the step.
    """
    script = home / "npm-block.sh"
    script.write_text(f"set -euo pipefail\n{_npm_block()}\necho {SENTINEL}\n")
    return subprocess.run(
        [BASH, str(script)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )


def _cache_dir(home: Path, name: str, apparent_bytes: int) -> Path:
    """A cache directory of a given apparent size, costing no real disk.

    `du -sb` reports apparent size, so a sparse file stands in for gigabytes
    that a test has no business actually writing.
    """
    d = home / ".npm" / name
    d.mkdir(parents=True)
    with (d / "blob").open("wb") as fh:
        fh.truncate(apparent_bytes)
    return d


@pytest.mark.skipif(BASH is None, reason="needs bash to run the block")
def test_a_cache_under_the_threshold_is_kept(tmp_path: Path):
    """The regression #314 is about, executed rather than read.

    The plugin's MCP server launches `npx -y socraticode` inside Claude Code's
    30s connect timeout. A wiped cache turns that into a ~1,700-tarball cold
    install, which does not fit — so the next session starts with no semantic
    index at all, while every health check still passes through the pin.
    """
    npx = _cache_dir(tmp_path, "_npx", 400 * 1024 * 1024)
    cacache = _cache_dir(tmp_path, "_cacache", 100 * 1024 * 1024)

    result = _run_npm_block(tmp_path)

    assert SENTINEL in result.stdout, f"the step aborted the run\nstderr: {result.stderr}"
    assert npx.exists() and cacache.exists(), (
        "a 500 MB cache was cleared — that is 17s of cold npx install charged to "
        "the next session's 30s MCP startup budget, to reclaim half a gigabyte"
    )


@pytest.mark.skipif(BASH is None, reason="needs bash to run the block")
def test_a_cache_over_the_threshold_is_still_cleared(tmp_path: Path):
    """The ceiling the threshold exists to keep: runaway caches still go."""
    npx = _cache_dir(tmp_path, "_npx", 3 * 1024 * 1024 * 1024)

    result = _run_npm_block(tmp_path)

    assert SENTINEL in result.stdout, f"the step aborted the run\nstderr: {result.stderr}"
    assert not npx.exists(), "a 3 GB cache survived the threshold"


@pytest.mark.skipif(BASH is None, reason="needs bash to run the block")
def test_an_absent_cache_does_not_abort_the_run(tmp_path: Path):
    """`du` on a path that is not there exits non-zero, and `set -e` is fatal.

    This is the state right after the step itself has run, so it is reached
    every second invocation rather than being an edge case.
    """
    result = _run_npm_block(tmp_path)

    assert SENTINEL in result.stdout, (
        "measuring an absent cache aborted the run; every step below it in "
        f"cleanup.sh would be skipped.\nstderr: {result.stderr}"
    )
    assert result.returncode == 0
