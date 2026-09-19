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

Asking systemd rather than probing also avoids waking a daemon on a host that
has disabled it: `docker.socket` inactive means no CLI call, so nothing
socket-activates `dockerd` plus `containerd` for ~120 MB on a 3.8 GiB swapless
box (#307, CannObserv/broker#17). Where the socket *is* active the prune does
wake the daemon — but there Docker is in use and the prune is the point.

**This file is scheduled for deletion by #310**, which removes the Docker branch
from the script and purges the packages from the VM. The helpers below say so in
their failure messages, so that removal produces an instruction rather than a
`StopIteration` traceback. `test_pipefail_is_what_makes_every_guard_load_bearing`
is the one assertion worth relocating rather than deleting with the rest.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLEANUP = REPO_ROOT / "scripts" / "cleanup.sh"

#: What a removal of the branch should tell the person who did it.
GONE = (
    "scripts/cleanup.sh has no Docker branch. If that was #310, delete this file — "
    "but relocate test_pipefail_is_what_makes_every_guard_load_bearing first."
)

#: Stands in for every step below the branch in the real script.
SENTINEL = "CLEANUP_REACHED_THE_END"

#: Resolved once, before the stub PATH replaces the real one.
BASH = shutil.which("bash")


def _docker_block() -> str:
    """The guard line through the end of its `if`, so assertions stay local.

    Reading the whole file instead would let an unrelated `systemctl is-active`
    somewhere else in the script satisfy the guard assertion below.
    """
    lines = CLEANUP.read_text().splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if "docker" in ln and ln.lstrip().startswith("if ")),
        None,
    )
    if start is None:
        pytest.fail(GONE)
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == "fi"), None)
    if end is None:
        pytest.fail(f"the Docker branch at line {start + 1} has no closing `fi`")
    return "\n".join(lines[start : end + 1])


def _exercise(
    tmp_path: Path, *, socket_active: bool, docker_exits: int
) -> subprocess.CompletedProcess:
    """Run the extracted block under `set -euo pipefail` against stubbed tools.

    The property under test is behavioural — "a failing prune cannot take the
    journal vacuum down with it" — and no reading of the text establishes it. So
    the block runs for real against a PATH holding nothing but the two commands
    it calls, with a sentinel appended for everything that would follow it. If
    the sentinel prints, the run survived. `echo` is a shell builtin, so the stub
    PATH needs nothing else.
    """
    binv = tmp_path / "bin"
    binv.mkdir()
    (binv / "systemctl").write_text(f"#!/bin/sh\nexit {0 if socket_active else 1}\n")
    (binv / "docker").write_text(f"#!/bin/sh\nexit {docker_exits}\n")
    for name in ("systemctl", "docker"):
        (binv / name).chmod(0o755)

    script = tmp_path / "block.sh"
    script.write_text(f"set -euo pipefail\n{_docker_block()}\necho {SENTINEL}\n")
    # bash by absolute path: PATH below holds only the two stubs, so the exec
    # lookup for the interpreter itself would miss it.
    return subprocess.run(
        [BASH, str(script)],
        env={"PATH": str(binv), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_pipefail_is_what_makes_every_guard_load_bearing():
    """The premise of this whole file. If it ever goes, re-derive the rest."""
    assert "set -euo pipefail" in CLEANUP.read_text()


def test_guard_names_the_docker_units_specifically():
    """`systemctl is-active` on its own proves nothing about Docker.

    A substring check for "systemctl" would be satisfied by a guard asking about
    any unit at all, so it is pinned to the two that decide whether a docker CLI
    call can succeed — and whether one would socket-activate the daemon.
    """
    block = _docker_block()
    assert "is-active" in block, "the branch must ask systemd, not probe with the binary"
    assert "docker.socket" in block and "docker.service" in block, (
        "the guard must name the docker units; `systemctl is-active` alone "
        "would be satisfied by a question about any unit"
    )


@pytest.mark.skipif(BASH is None, reason="needs bash to run the block")
def test_a_failing_prune_does_not_abort_the_run(tmp_path: Path):
    """The regression #300's teardown actually caused, executed rather than read.

    Socket active, so the guard opens; `docker image prune` then exits non-zero
    the way it does against a daemon that is not listening. Under `set -e` that
    ends the script — unless the call tolerates its own failure.
    """
    result = _exercise(tmp_path, socket_active=True, docker_exits=1)
    assert SENTINEL in result.stdout, (
        "a failing `docker image prune` aborted the run under set -e; every step "
        f"below it in cleanup.sh would be skipped.\nstderr: {result.stderr}"
    )
    assert result.returncode == 0


@pytest.mark.skipif(BASH is None, reason="needs bash to run the block")
def test_the_branch_is_skipped_when_no_docker_unit_is_up(tmp_path: Path):
    """The state this host is in since #300, and the one #310 makes permanent.

    A stub `docker` that exits 0 would mask a guard that ran the branch anyway,
    so it exits non-zero: reaching the end here proves the prune was never
    called, not merely that it succeeded.
    """
    result = _exercise(tmp_path, socket_active=False, docker_exits=1)
    assert SENTINEL in result.stdout
    assert "Docker dangling images" not in result.stdout, (
        "the branch ran with no docker unit active — which on this host means a "
        "CLI call that would socket-activate a daemon nothing needs"
    )


def test_prune_tolerates_its_own_failure_in_the_source():
    """Belt to the behavioural tests' braces, and a clearer diff when it breaks.

    Those tests prove the property; this one names the mechanism, so a reviewer
    who deletes `|| true` sees *why* it was there rather than only that
    something downstream went red.
    """
    prune = next((ln for ln in _docker_block().splitlines() if "prune" in ln), None)
    if prune is None:
        pytest.fail(GONE)
    assert prune.rstrip().endswith("|| true"), (
        f"{prune.strip()!r} aborts the whole cleanup under set -e if it fails"
    )
