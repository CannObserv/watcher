"""Guard: watcher's own disk cleanup is retired (#385).

``scripts/cleanup.sh`` and ``watcher-cleanup.timer`` pruned this host weekly
from 2026-04 (#122) to 2026-11. The cohort disk GC replaced them
(``tests/deploy/test_disk_gc.py``), enabled in the same change that removed
them, so the host never ran two GCs or none. A second one coming back would
race the GC over the same trees, and the sudoers rules it needed would be root
for nothing.

What the old script did that no GC tier does was dropped on purpose (#385):
``apt-get clean``, the 14-day journal vacuum (the GC's journald drop-in caps
the journal at 512M) and the Playwright audit (no cache on this host). apt
cleans its own cache here: the image's ``/etc/apt/apt.conf.d/docker-clean``
deletes ``archives/*.deb`` after every dpkg run and every ``apt update``, so
the weekly clean found 16-20K. Nothing cleans it if that hook goes, and
patching-hosts downloads with ``apt-get install -d``, so the hook is pinned.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_UNIT = Path("/etc/systemd/system/watcher.service")
APT_CLEAN_HOOK = Path("/etc/apt/apt.conf.d/docker-clean")

RETIRED_REPO_PATHS = (
    "scripts/cleanup.sh",
    "deploy/watcher-cleanup.service",
    "deploy/watcher-cleanup.timer",
    "deploy/watcher-cleanup.sudoers",
    "tests/deploy/test_cleanup_script.py",
)
RETIRED_HOST_PATHS = (
    "/etc/systemd/system/watcher-cleanup.service",
    "/etc/systemd/system/watcher-cleanup.timer",
    "/etc/systemd/system/timers.target.wants/watcher-cleanup.timer",
    "/etc/sudoers.d/watcher-cleanup",
    "/var/log/watcher",
)


@pytest.mark.parametrize("path", RETIRED_REPO_PATHS)
def test_the_repo_carries_no_cleanup(path: str) -> None:
    assert not (REPO_ROOT / path).exists(), f"{path} is back: #385 retired it"


def test_the_knob_names_no_cleanup() -> None:
    knob = (REPO_ROOT / ".skills" / "patching-hosts").read_text()
    for name in ("watcher-cleanup", "cleanup.sh"):
        assert name not in knob, f"the knob names {name} again: #385 retired it"


@pytest.mark.parametrize("path", RETIRED_HOST_PATHS)
def test_the_host_carries_no_cleanup(path: str) -> None:
    if not SERVICE_UNIT.exists():
        pytest.skip(f"{SERVICE_UNIT} not present: not a host running the service")
    assert not Path(path).exists(), (
        f"{path} is still on the host: #385 retired it. Retire it with the "
        "maintaining-host-disk skill (references/retiring.md)"
    )


def test_apt_still_cleans_its_own_cache() -> None:
    """Why ``apt-get clean`` could go: the hook does it after every dpkg run."""
    if not SERVICE_UNIT.exists():
        pytest.skip(f"{SERVICE_UNIT} not present: not a host running the service")
    assert APT_CLEAN_HOOK.is_file(), (
        f"{APT_CLEAN_HOOK} is gone: nothing cleans /var/cache/apt/archives now (#385)"
    )
    hook = APT_CLEAN_HOOK.read_text()
    assert "DPkg::Post-Invoke" in hook and "/var/cache/apt/archives/*.deb" in hook
