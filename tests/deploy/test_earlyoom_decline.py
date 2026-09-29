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

import pytest

INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")
EARLYOOM = "earlyoom.service"

# What exe.dev starts a session from; the session's adj is theirs to set.
SESSION_PARENTS = frozenset({"exe-init", "sshd"})
# PID 1's own cgroup. A session outliving its exe-init ancestor is reparented to
# PID 1 and stays here; a service under PID 1 sits in system.slice instead.
SESSION_CGROUP = "/init.scope"
# What exe-init 14fd603 starts a session at (#337).
SESSION_ADJ = 0


def _on_host() -> bool:
    return INSTALLED_UNIT.exists()


def _session_root_adj(proc: Path, pid: int) -> int | None:
    """``oom_score_adj`` of the process exe.dev started ``pid``'s session from.

    Walks the ancestry to the first process whose parent is in
    ``SESSION_PARENTS`` and reads that one, not ``pid`` itself: a leaf can be
    ``choom``'d (HOST-MEMORY.md step 1's capped installs are), the session root
    cannot. ``None`` when no ancestor is a session.
    """
    while pid > 1:
        status = (proc / str(pid) / "status").read_text()
        ppid = int(re.search(r"^PPid:\s*(\d+)", status, flags=re.MULTILINE).group(1))
        if ppid < 1:
            return None
        if (proc / str(ppid) / "comm").read_text().strip() in SESSION_PARENTS:
            return int((proc / str(pid) / "oom_score_adj").read_text())
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
    assert _session_root_adj(tmp_path, 900) == 0


def test_a_session_under_sshd_reports_its_root(tmp_path: Path) -> None:
    """notifier's shape: only ``sshd`` at -1000, the session under it at 0."""
    _fake_process(tmp_path, 217, 1, "sshd", -1000)
    _fake_process(tmp_path, 700, 217, "bash", 0)
    _fake_process(tmp_path, 701, 700, "python3", 0)
    assert _session_root_adj(tmp_path, 701) == 0


def test_an_orphaned_session_reports_its_root(tmp_path: Path) -> None:
    """#333: a session whose ``exe-init`` ancestor exited, reparented to PID 1.

    Measured on co-watcher 2026-09-28: the chain ended ``sh`` → PID 1, every
    process in it in ``/init.scope``. Still a session, and its adj still counts.
    """
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 645, 1, "sh", -1000)
    _fake_process(tmp_path, 649, 645, "MainThread", -1000)
    assert _session_root_adj(tmp_path, 649) == -1000


def test_a_service_under_pid_1_is_not_a_session(tmp_path: Path) -> None:
    """The same parent as an orphan; ``system.slice`` tells them apart."""
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 400, 1, "uv", -500, "/system.slice/watcher.service")
    _fake_process(tmp_path, 401, 400, "uvicorn", -500, "/system.slice/watcher.service")
    assert _session_root_adj(tmp_path, 401) is None


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
    assert _session_root_adj(tmp_path, 316) is None


def test_sessions_here_are_killable() -> None:
    """The premise of the decline, read off this session's own root.

    Fails rather than skips on the host (#333): gate on the host, never on the
    thing under test.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    adj = _session_root_adj(Path("/proc"), os.getpid())
    assert adj is not None, (
        "no exe.dev session root above this process — neither a child of "
        f"{sorted(SESSION_PARENTS)} nor a PID 1 child in {SESSION_CGROUP}. Run it "
        "from an agent or SSH session; if it was run from one, exe.dev changed how "
        "a session starts and this walk needs to learn it (#333)"
    )
    assert adj == SESSION_ADJ, (
        f"this session's root reads oom_score_adj={adj}, not {SESSION_ADJ}: exe.dev "
        "starts sessions exempt again, so the kernel can no longer pick one and "
        "watcher is the largest candidate left. Check `exe-init --version` against "
        "#337 and revisit the earlyoom decline in docs/HOST-MEMORY.md §3"
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

    On a -1000 host it can only work down the list of what the kernel would
    take anyway, starting sooner: watcher's database connections, then watcher.
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
