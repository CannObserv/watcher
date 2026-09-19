"""The weekly cleanup must not abort on a Docker daemon that is not there (#300).

`scripts/cleanup.sh` runs under `set -euo pipefail`, so a non-zero exit anywhere
in it ends the run. The Docker branch sits in the middle, which makes the guard
in front of it load-bearing for every step *after* it — the journal vacuum, the
Playwright audit and the closing disk report.

The original guard was `command -v docker`, which asks whether the binary is
installed. That was true and sufficient while the daemon ran here. #300 moved
the semantic index to the shared store on `co-index` and tore Docker down, so
the binary is still installed and `docker image prune -f` now exits non-zero
with "Cannot connect to the Docker daemon" — aborting the weekly cleanup from
that line onward, and doing it quietly, since the timer only reports the unit's
exit status and the log looks like a run that simply ended.

Asking systemd rather than probing is also the cohort's stated lesson: while
`docker.socket` is enabled, *any* docker CLI call — `docker ps` included —
socket-activates `dockerd` and `containerd` for ~120 MB. On a 3.8 GiB host with
no swap and a production unit resident, a probe that starts a daemon is not a
free question to ask (#307, CannObserv/broker#17).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLEANUP = REPO_ROOT / "scripts" / "cleanup.sh"


def _docker_block() -> str:
    """The guard line through the end of its `if`, so assertions stay local.

    Reading the whole file instead would let an unrelated `systemctl is-active`
    somewhere else in the script satisfy the guard assertion below.
    """
    lines = CLEANUP.read_text().splitlines()
    start = next(
        i for i, ln in enumerate(lines) if "docker" in ln and ln.lstrip().startswith("if ")
    )
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
    return "\n".join(lines[start : end + 1])


def test_pipefail_is_what_makes_the_guard_load_bearing():
    """The premise of this whole file. If it ever goes, re-derive the rest."""
    assert "set -euo pipefail" in CLEANUP.read_text()


def test_docker_branch_asks_systemd_not_just_the_binary():
    """`command -v docker` is true on a host whose daemon is gone and disabled."""
    block = _docker_block()
    assert "systemctl" in block, (
        "the Docker branch must ask systemd whether the daemon is actually up; "
        "`command -v docker` only proves the binary is installed"
    )
    assert "is-active" in block


def test_docker_branch_cannot_abort_the_run():
    """Belt to the guard's braces: a race between the check and the call.

    systemd can stop the socket in the window between the guard and the prune,
    and `docker image prune` can fail for reasons unrelated to the daemon. Under
    `set -e` either one takes the journal vacuum down with it, so the call
    tolerates its own failure rather than relying on the guard being exhaustive.
    """
    block = _docker_block()
    prune = next(ln for ln in block.splitlines() if "prune" in ln)
    assert prune.rstrip().endswith("|| true"), (
        f"{prune.strip()!r} aborts the whole cleanup under set -e if it fails"
    )
