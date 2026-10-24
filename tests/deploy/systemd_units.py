"""Parse systemd unit text, and read a running unit, the way the deploy tests need.

Shared by ``test_installed_unit_matches_repo`` and ``test_memory_dropins``: both
read ``deploy/`` unit files, and a sign or suffix parsed differently in each
would let one assert what the other contradicts. ``test_memory_dropins`` and
``test_earlyoom_decline`` both read a unit's live ``oom_score_adj``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def directive_values(unit_text: str, directive: str) -> list[str]:
    """Return every value a directive is given, verbatim and in file order.

    Verbatim because a leading ``-`` is not punctuation in general: it is an
    ``EnvironmentFile=`` prefix meaning "skip if missing", but it is the sign of
    the number in ``OOMScoreAdjust=-500``. Stripping it here once cost that
    assertion its meaning — it read -500 as 500 and passed a unit that had made
    the service *more* attractive to the OOM killer, not less. The one caller
    that wants the prefix gone strips it itself.
    """
    prefix = f"{directive}="
    values: list[str] = []
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            values.append(stripped.removeprefix(prefix))
    return values


def memory_size_to_bytes(value: str) -> int:
    """Parse a systemd memory size (``512M``, ``1G``, ``1048576``) into bytes.

    Only the suffixes systemd itself documents for ``MemoryLow=`` are accepted.
    An unparseable value raises rather than defaulting to zero: a typo in a
    reservation must fail this test loudly, not quietly assert nothing.
    """
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    suffix = value[-1:].upper()
    if suffix in multipliers:
        return int(value[:-1]) * multipliers[suffix]
    return int(value)


def time_span_to_seconds(value: str) -> float:
    """Parse a systemd time span given in seconds (``90``, ``90s``).

    Only the plain-seconds forms are accepted. ``1min 30s`` and the other
    compound spans systemd also reads raise rather than being misread: a stop
    budget parsed as 1 would fail the budget test for the wrong reason, and one
    parsed as 0 could pass it.
    """
    number = value.removesuffix("s")
    if not number.isdigit():
        raise ValueError(f"unsupported time span {value!r}: write it in seconds")
    return float(number)


def live_main_pid_adj(unit: str) -> int | None:
    """``oom_score_adj`` of ``unit``'s running main process; ``None`` when it is down.

    The process, not the unit: ``OOMScoreAdjust=`` applies at exec, so after a
    ``daemon-reload`` the unit reports the new value while the running process
    keeps the old one until it restarts. Skips where there is no ``systemctl``.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        pytest.skip("systemctl not available")
    pid = subprocess.run(
        [systemctl, "show", unit, "--property=MainPID", "--value"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not pid or pid == "0":
        return None
    return int(Path(f"/proc/{pid}/oom_score_adj").read_text())
