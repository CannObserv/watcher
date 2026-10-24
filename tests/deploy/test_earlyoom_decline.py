"""earlyoom is declined on this host (#323), for a reason #337 changed.

#307 installed earlyoom to pick the ``npm``/``node`` process that was actually
spiking. Until #337 it could not: ``exe-init`` build 8579326 started every
session at ``oom_score_adj`` -1000, and earlyoom 1.7 skips a -1000 process
exactly as the kernel does. exe-init 14fd603 (swapped in 2026-09-29) starts
sessions at 0, so the kernel's own order is now the right one — the sessions
rank ahead of watcher at -500 — and earlyoom's only addition would be to fire
at 10% available, where the kernel waits for memory to actually run out.
Measured after the restart: a tuned ``--prefer`` picks the editor's extension
host the agent session runs under. CannObserv/replicator#112 declined it too.

Both halves are pinned live, on the host only. earlyoom must not be running:
``apt install earlyoom`` starts it at once, on stock arguments. And sessions
must read 0, because that premise is exe.dev's, not this repo's: the -1000
came from their ``exe-init`` build and a rebuilt VM could bring it back. If a
session here reads -1000 again, the kernel can no longer pick it and watcher
is the largest candidate left (docs/HOST-MEMORY.md).

On the host the premise check fails rather than skips when it cannot find a
session root (#333): a skip there asserts nothing on the one host it exists for.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.deploy.systemd_units import live_main_pid_adj

INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")
EARLYOOM = "earlyoom.service"
WATCHER = "watcher.service"

# What exe.dev starts a session from; the session's adj is theirs to set.
SESSION_PARENTS = frozenset({"exe-init", "sshd"})
# PID 1's own cgroup. A session outliving its exe-init ancestor is reparented to
# PID 1 and stays here; a service under PID 1 sits in system.slice instead.
SESSION_CGROUP = "/init.scope"
# What exe-init 14fd603 starts a session at (#337).
SESSION_ADJ = 0
# exe.dev's Shelley agent: each conversation's shell runs inside this unit (#399).
SHELLEY_CGROUP = "/system.slice/shelley.service"


def _on_host() -> bool:
    return INSTALLED_UNIT.exists()


class SessionRoot(NamedTuple):
    """The process exe.dev started a session from, and what the kernel sees."""

    pid: int
    adj: int
    cgroup: str


def _cgroup(proc: Path, pid: int) -> str:
    return (proc / str(pid) / "cgroup").read_text().strip().removeprefix("0::")


def _session_root(proc: Path, pid: int) -> SessionRoot | None:
    """The process exe.dev started ``pid``'s session from.

    Walks the ancestry to the first process whose parent is in
    ``SESSION_PARENTS`` and reads that one, not ``pid`` itself: a leaf can be
    ``choom``'d (HOST-MEMORY.md step 1's capped installs are), the session root
    cannot. A PID 1 child in ``SESSION_CGROUP`` is a root too — a session whose
    ``exe-init`` ancestor exited (#333). ``None`` when no ancestor is a session.
    """
    while pid > 1:
        status = (proc / str(pid) / "status").read_text()
        ppid = int(re.search(r"^PPid:\s*(\d+)", status, flags=re.MULTILINE).group(1))
        if ppid < 1:
            return None
        if (proc / str(ppid) / "comm").read_text().strip() in SESSION_PARENTS or (
            ppid == 1 and _cgroup(proc, pid) == SESSION_CGROUP
        ):
            adj = int((proc / str(pid) / "oom_score_adj").read_text())
            return SessionRoot(pid, adj, _cgroup(proc, pid))
        pid = ppid
    return None


def _fake_process(
    proc: Path, pid: int, ppid: int, comm: str, adj: int, cgroup: str = SESSION_CGROUP
) -> None:
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "status").write_text(f"Name:\t{comm}\nPPid:\t{ppid}\n")
    (proc / str(pid) / "comm").write_text(f"{comm}\n")
    (proc / str(pid) / "oom_score_adj").write_text(f"{adj}\n")
    (proc / str(pid) / "cgroup").write_text(f"0::{cgroup}\n")


def test_a_session_under_exe_init_reports_its_root(tmp_path: Path) -> None:
    """The root, not the leaf: a leaf can be ``choom``'d, the root cannot."""
    _fake_process(tmp_path, 217, 1, "exe-init", -1000)
    _fake_process(tmp_path, 540, 217, "bash", 0)
    _fake_process(tmp_path, 900, 540, "npm install", 500)
    assert _session_root(tmp_path, 900) == SessionRoot(540, 0, SESSION_CGROUP)


def test_a_session_under_sshd_reports_its_root(tmp_path: Path) -> None:
    """notifier's shape: only ``sshd`` at -1000, the session under it at 0."""
    _fake_process(tmp_path, 217, 1, "sshd", -1000)
    _fake_process(tmp_path, 700, 217, "bash", 0)
    _fake_process(tmp_path, 701, 700, "python3", 0)
    assert _session_root(tmp_path, 701) == SessionRoot(700, 0, SESSION_CGROUP)


def test_an_orphaned_session_reports_its_root(tmp_path: Path) -> None:
    """#333: a session whose ``exe-init`` ancestor exited, reparented to PID 1.

    Measured on co-watcher 2026-09-28: the chain ended ``sh`` → PID 1, every
    process in it in ``/init.scope``. Still a session, and its adj still counts.
    """
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 645, 1, "sh", -1000)
    _fake_process(tmp_path, 649, 645, "MainThread", -1000)
    assert _session_root(tmp_path, 649) == SessionRoot(645, -1000, SESSION_CGROUP)


def test_a_service_under_pid_1_is_not_a_session(tmp_path: Path) -> None:
    """The same parent as an orphan; ``system.slice`` tells them apart."""
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 400, 1, "uv", -500, "/system.slice/watcher.service")
    _fake_process(tmp_path, 401, 400, "uvicorn", -500, "/system.slice/watcher.service")
    assert _session_root(tmp_path, 401) is None


def test_a_shelley_session_reports_its_root(tmp_path: Path) -> None:
    """#399: exe.dev's Shelley agent runs each conversation's shell in its unit.

    Measured on co-watcher 2026-10-24: ``bash`` → ``bash`` → ``shelley`` (PID 1
    child), every process in ``/system.slice/shelley.service`` at -1. The root
    is the server, not the leaf, for the same reason as under ``exe-init``.
    """
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 573, 1, "shelley", -1, SHELLEY_CGROUP)
    _fake_process(tmp_path, 652, 573, "bash", -1, SHELLEY_CGROUP)
    _fake_process(tmp_path, 653, 652, "npm install", 500, SHELLEY_CGROUP)
    assert _session_root(tmp_path, 653) == SessionRoot(573, -1, SHELLEY_CGROUP)


def test_an_orphaned_shelley_session_reports_its_root(tmp_path: Path) -> None:
    """The unit is ``KillMode=process``: a Shelley restart kills only the server.

    Its sessions' shells are reparented to PID 1 and stay in the unit's cgroup —
    #333's orphan, inside a unit instead of ``/init.scope``.
    """
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 652, 1, "bash", -1, SHELLEY_CGROUP)
    _fake_process(tmp_path, 653, 652, "python3", 0, SHELLEY_CGROUP)
    assert _session_root(tmp_path, 653) == SessionRoot(652, -1, SHELLEY_CGROUP)


def test_another_service_named_like_a_session_is_not_one(tmp_path: Path) -> None:
    """Only the named unit counts — never "any system service" (#399).

    A process called ``shelley`` in some other unit is that unit's, and its adj
    is that unit's choice, not a session's.
    """
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 800, 1, "shelley", -1, "/system.slice/shelley-dev.service")
    _fake_process(tmp_path, 801, 800, "bash", -1, "/system.slice/shelley-dev.service")
    assert _session_root(tmp_path, 801) is None


def test_no_session_ancestor_is_none(tmp_path: Path) -> None:
    """CI, cron, the user manager: nothing to measure.

    The user manager's own cgroup ends in ``init.scope`` too; only PID 1's counts.
    """
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(
        tmp_path, 315, 1, "systemd", 100, "/user.slice/user-1000.slice/user@1000.service/init.scope"
    )
    _fake_process(
        tmp_path, 316, 315, "python3", 0, "/user.slice/user-1000.slice/user@1000.service/app.slice"
    )
    assert _session_root(tmp_path, 316) is None


def test_sessions_here_are_killable() -> None:
    """The premise of the decline, read off this session's own root.

    The property #337 protects: the kernel can pick the session (above -1000),
    and picks it before watcher (above watcher's live adj). Both read off the
    host, never off what a harness is assumed to be. Sessions exe-init starts
    must also read exactly 0 — that build's premise; Shelley's adj is its unit's.

    Fails rather than skips on the host (#333): gate on the host, never on the
    thing under test.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    root = _session_root(Path("/proc"), os.getpid())
    assert root is not None, (
        "no exe.dev session root above this process — neither a child of "
        f"{sorted(SESSION_PARENTS)} nor a PID 1 child in {SESSION_CGROUP} or "
        f"{SHELLEY_CGROUP}. Run it from an agent or SSH session; if it was run from "
        "one, exe.dev changed how a session starts and this walk needs to learn it "
        "(#333, #399)"
    )
    watcher = live_main_pid_adj(WATCHER)
    assert watcher is not None, f"{WATCHER} is not running — start it, then rerun"
    assert -1000 < root.adj and watcher < root.adj, (
        f"this session's root (pid {root.pid}, {root.cgroup}) reads "
        f"oom_score_adj={root.adj}; watcher reads {watcher}. The kernel can no "
        "longer pick the session ahead of watcher, which is the largest candidate "
        "left. Check `/exe.dev/bin/exe-init --version` against #337 (and, for "
        f"{SHELLEY_CGROUP}, the unit's OOMScoreAdjust — exe.dev's, never patched "
        "here, #399), then revisit the earlyoom decline in docs/HOST-MEMORY.md §3"
    )
    if root.cgroup != SHELLEY_CGROUP:
        assert root.adj == SESSION_ADJ, (
            f"this session's root reads oom_score_adj={root.adj}, not {SESSION_ADJ}: "
            "exe-init no longer starts sessions where #337 measured them. Check "
            "`/exe.dev/bin/exe-init --version` against #337 and revisit the earlyoom "
            "decline in docs/HOST-MEMORY.md §3"
        )


def _systemctl(*args: str) -> str:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        pytest.skip("systemctl not available")
    return subprocess.run(
        [systemctl, *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def test_earlyoom_is_not_running_here() -> None:
    """Off, and staying off across a reboot.

    With sessions at 0 the kernel already picks a spiking session ahead of
    watcher; earlyoom would only pick it sooner, at 10% available, before
    anything has failed. On a -1000 host it is worse: it works down the kernel's
    list starting sooner — watcher's database connections, then watcher.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    fix = f"sudo systemctl disable --now {EARLYOOM}  (docs/HOST-MEMORY.md §3, #323)"
    assert _systemctl("is-active", EARLYOOM) in ("inactive", "failed"), (
        f"{EARLYOOM} is running. Stop it: {fix}"
    )
    # Allow-listed: enabled-runtime, alias, linked and indirect all start it too.
    # not-found is a purged package.
    state = _systemctl("is-enabled", EARLYOOM)
    assert state in ("disabled", "masked", "not-found"), f"{EARLYOOM} is {state}. Disable it: {fix}"
