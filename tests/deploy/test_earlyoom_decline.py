"""earlyoom is declined on this host, on a premise that belongs to exe.dev (#323).

#307 installed earlyoom to pick the ``npm``/``node`` process that was actually
spiking, "at the default 0". It cannot here. exe.dev starts every session from
``exe-init`` or ``sshd`` at ``oom_score_adj`` -1000, everything a session
launches inherits it, and earlyoom 1.7 skips a -1000 process exactly as the
kernel does, ``--prefer`` or not (``kill.c``, after the bonus is added). What
it can reach is the small daemons, then postgres's backends, ``tailscaled`` and
watcher itself, starting at 10% available, when the kernel would take none of
them until memory actually ran out. CannObserv/replicator#112 declined it on the
same class of host.

Both halves are pinned live, on the host only. earlyoom must not be running:
``apt install earlyoom`` starts it at once, on stock arguments. And sessions
must still read -1000, because that premise is not this repo's to hold:
notifier's sessions sit at 0, and what decides it was never determined. If a
session here stops reading -1000, the decline no longer holds.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")
EARLYOOM = "earlyoom.service"

# What exe.dev starts a session from; the -1000 is theirs, inherited or not.
SESSION_PARENTS = frozenset({"exe-init", "sshd"})
OOM_EXEMPT = -1000


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


def _fake_process(proc: Path, pid: int, ppid: int, comm: str, adj: int) -> None:
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "status").write_text(f"Name:\t{comm}\nPPid:\t{ppid}\n")
    (proc / str(pid) / "comm").write_text(f"{comm}\n")
    (proc / str(pid) / "oom_score_adj").write_text(f"{adj}\n")


def test_a_session_under_exe_init_reports_its_root(tmp_path: Path) -> None:
    """The root, not the leaf: a leaf can be ``choom``'d, the root cannot."""
    _fake_process(tmp_path, 218, 1, "exe-init", -1000)
    _fake_process(tmp_path, 576, 218, "bash", -1000)
    _fake_process(tmp_path, 900, 576, "npm install", 500)
    assert _session_root_adj(tmp_path, 900) == -1000


def test_a_session_under_sshd_reports_its_root(tmp_path: Path) -> None:
    """notifier's shape: only ``sshd`` at -1000, the session under it at 0."""
    _fake_process(tmp_path, 217, 1, "sshd", -1000)
    _fake_process(tmp_path, 700, 217, "bash", 0)
    _fake_process(tmp_path, 701, 700, "python3", 0)
    assert _session_root_adj(tmp_path, 701) == 0


def test_no_session_ancestor_is_none(tmp_path: Path) -> None:
    """CI, a systemd unit, cron: nothing to measure."""
    _fake_process(tmp_path, 1, 0, "systemd", 0)
    _fake_process(tmp_path, 315, 1, "systemd", 100)
    _fake_process(tmp_path, 316, 315, "python3", 0)
    assert _session_root_adj(tmp_path, 316) is None


def test_sessions_here_are_still_exempt() -> None:
    """The premise of the decline, read off this session's own root."""
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    adj = _session_root_adj(Path("/proc"), os.getpid())
    if adj is None:
        pytest.skip("not run from an exe.dev session")
    assert adj == OOM_EXEMPT, (
        f"this session's root reads oom_score_adj={adj}, not {OOM_EXEMPT}: exe.dev no "
        "longer exempts sessions here, so earlyoom's --prefer can now reach them and "
        "#323's decline no longer holds. Revisit it in docs/HOST-MEMORY.md §3"
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
    take anyway, starting sooner: postgres through crash recovery, then watcher.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    fix = f"sudo systemctl disable --now {EARLYOOM}  (docs/HOST-MEMORY.md §3, #323)"
    assert _systemctl("is-active", EARLYOOM) in ("inactive", "failed"), (
        f"{EARLYOOM} is running. Stop it: {fix}"
    )
    assert _systemctl("is-enabled", EARLYOOM) != "enabled", (
        f"{EARLYOOM} is enabled. Disable it: {fix}"
    )
