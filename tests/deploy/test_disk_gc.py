"""The cohort disk GC on this host: the config it runs and how it got here (#385).

The GC is Provisioner's (CannObserv/provisioner#61), installed from the pinned
co-image-base layer by the vendored ``maintaining-host-disk`` skill. Its code and
units live under ``/usr/local/lib`` and are never edited here; what this repo
owns is the host's config, tracked as ``deploy/disk-gc.conf`` and installed to
``/etc/cannobserv/disk-gc/disk-gc.conf``:

- **The tiers are the template's,** docker and worktrees off: Docker left this
  host in #336, and worktrees are made and destroyed by ``using-git-worktrees``.
- **The pinned SocratiCode tree is kept.** ``npx_spec_files`` names this repo's
  ``.claude/settings.json``, so the ``npx`` tier keeps the tree the session
  launches from. The ``npm-cache`` tier wipes ``~/.npm/_cacache`` daily, which
  #314 once forbade: an exact-version spec found in the ``_npx`` tree installs
  nothing, so only that tree has to survive.
- **The lines are 85/80,** which patching-hosts also reads as its floor
  (gregoryfoster/skills#390).
- **The installed copy is the tracked one,** checked on the host the way
  ``test_installed_pins_match_tracked`` checks the apt pins.

Off the host the installed checks skip; on it they fail, so a host that never
received the config can't pass for one that did.
"""

import configparser
import json
import os
import re
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED = REPO_ROOT / "deploy" / "disk-gc.conf"
INSTALLED = Path("/etc/cannobserv/disk-gc/disk-gc.conf")
GC_DIR = Path("/usr/local/lib/cannobserv/disk-gc")
GC = GC_DIR / "disk-gc"
TIMERS = ("cannobserv-disk-gc.timer", "cannobserv-disk-headroom.timer")
#: The deploy checkout: the GC reads the spec from there, not from a worktree.
DEPLOY_CHECKOUT = Path("/home/exedev/watcher")
SERVICE_UNIT = Path("/etc/systemd/system/watcher.service")
SKILL = "maintaining-host-disk"
VERSION = "4"

TEMPLATE_TIERS = {
    "vscode-servers",
    "vscode-clis",
    "claude-extensions",
    "vsix-cache",
    "plugin-cache",
    "npx",
    "npm-cache",
    "pytest-garbage",
    "uv",
}


def _on_host() -> bool:
    return SERVICE_UNIT.exists()


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, default_section="\0none")
    parser.read_string(TRACKED.read_text())
    return parser


def test_the_tiers_are_the_templates_with_docker_and_worktrees_off() -> None:
    gc = _config()["gc"]
    assert set(gc["tiers"].split()) == TEMPLATE_TIERS
    assert gc["worktree_repos"].strip() == ""
    assert gc["homes"].split() == ["/home/exedev"]


def test_the_pinned_socraticode_tree_is_kept() -> None:
    """The spec file is the deploy checkout's settings, and it pins a version."""
    files = _config()["gc"]["npx_spec_files"].split()
    assert files == [str(DEPLOY_CHECKOUT / ".claude" / "settings.json")]
    spec = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())["env"][
        "SOCRATICODE_SPEC"
    ]
    # An exact version: a range would reinstall into the tree on a new release.
    name, _, version = spec.partition("@")
    assert name == "socraticode" and version.count(".") == 2, spec
    assert all(part.isdigit() for part in version.split(".")), spec


def test_the_lines_are_85_and_80() -> None:
    sensor = _config()["sensor"]
    assert sensor["mount"] == "/"
    assert int(sensor["alert_at_percent"]) == 85
    assert int(sensor["clear_at_percent"]) == 80
    # co-status's check-in port (#330); the host is the config's to name, not a
    # test's (test_no_resolvable_service_hosts).
    url = urlsplit(sensor["status_url"])
    assert url.scheme == "http" and url.hostname and url.port == 9000, url.geturl()


def test_the_sensor_checks_in_to_its_monitor() -> None:
    """Set before the timers (the skill's turning-on.md): with no id, every
    sensor run logs ``not checking in`` and exits 0, so no fault reaches
    anyone, and an enabled monitor pages *has stopped reporting*."""
    monitor_id = _config()["sensor"]["monitor_id"].strip()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", monitor_id), (
        f"monitor_id {monitor_id!r} is not co-watcher-disk-headroom's ULID (CannObserv/status#112)"
    )


def test_the_skill_is_linked() -> None:
    """docs/SKILLS.md: both discovery paths (test_skill_links checks the shape)."""
    assert os.readlink(REPO_ROOT / "skills" / SKILL) == (
        f"../skills-vendor/gregoryfoster-skills/skills/{SKILL}"
    )
    assert os.readlink(REPO_ROOT / ".claude" / "skills" / SKILL) == f"../../skills/{SKILL}"


def _host_only() -> None:
    if not _on_host():
        pytest.skip(f"{SERVICE_UNIT} not present: not a host running the service")


def test_the_installed_gc_is_the_pinned_version() -> None:
    _host_only()
    assert GC.is_file(), f"{GC} missing: install it with the {SKILL} skill"
    assert (GC_DIR / "VERSION").read_text().strip() == VERSION


def test_the_installed_config_is_the_tracked_one() -> None:
    _host_only()
    hint = f"Install with:\n  sudo install -D -m 0644 -o root -g root {TRACKED} {INSTALLED}"
    assert INSTALLED.exists(), f"{INSTALLED} is missing.\n{hint}"
    assert INSTALLED.read_text() == TRACKED.read_text(), f"{INSTALLED} drifted.\n{hint}"


def test_the_installed_gc_accepts_the_tracked_config() -> None:
    """Loaded with bytecode off: as root a ``__pycache__`` beside ``disk-gc``
    is a file MANIFEST doesn't list (the skill's verifying.md, #514)."""
    _host_only()
    before = sorted(p.name for p in GC_DIR.iterdir())
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader = SourceFileLoader("cannobserv_disk_gc", str(GC))
        module = module_from_spec(spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        cfg = module.load_config(str(TRACKED))
    finally:
        sys.dont_write_bytecode = previous
    assert sorted(p.name for p in GC_DIR.iterdir()) == before
    assert cfg.alert_at_percent == 85 and cfg.clear_at == 80
    assert cfg.tiers == frozenset(TEMPLATE_TIERS)


def test_both_timers_are_enabled() -> None:
    """Enabled in the change that retired scripts/cleanup.sh (#385)."""
    _host_only()
    for timer in TIMERS:
        state = subprocess.run(
            ["systemctl", "is-enabled", timer], capture_output=True, text=True
        ).stdout.strip()
        assert state == "enabled", f"{timer} is {state or 'unknown'}"
