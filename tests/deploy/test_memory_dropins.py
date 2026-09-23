"""Memory reservations for watcher's dependency chain (#309).

#307 gave ``watcher.service`` a ``MemoryLow=`` reservation. Killing or starving
what watcher *depends on* is the same outage by another route, so postgres and
``tailscaled`` take one too — and so do the slices above them, which is the part
#307 missed.

cgroup v2 bounds a cgroup's *effective* ``memory.low`` by the effective low of
every ancestor. ``system.slice`` shipped at 0 and ``cgroup2`` here is mounted by
``exe-init`` without ``memory_recursiveprot``, so ``watcher.service``'s 512M was
written, reported by ``systemctl show``, and protected nothing. The competitor
is ``init.scope`` — exe.dev's agent sessions, a root-level sibling of
``system.slice`` — so the slice's reservation is what actually moves reclaim
pressure onto the sessions.

The drop-ins live in ``deploy/dropins/`` as ``<unit>.d/<file>``, installed to
``/etc/systemd/system/`` under the same relative path. The repo-side checks run
everywhere; the installed and live checks skip on a host that does not run the
service, like the other drift checks in this package.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy.systemd_units import directive_values, memory_size_to_bytes

REPO = Path(__file__).resolve().parents[2]
DROPINS = REPO / "deploy" / "dropins"
WATCHER_UNIT = REPO / "deploy" / "watcher.service"
INSTALLED = Path("/etc/systemd/system")
CGROUP_ROOT = Path("/sys/fs/cgroup")
DROPIN_NAME = "10-watcher-memory.conf"

SYSTEM_SLICE = "system.slice"
POSTGRES_SLICE = "system-postgresql.slice"
# The template, not an instance: a major upgrade renames the running unit.
POSTGRES = "postgresql@.service"
TAILSCALED = "tailscaled.service"
WATCHER = "watcher.service"

# Every drop-in this issue ships, keyed by the unit it attaches to.
TARGETS = (SYSTEM_SLICE, POSTGRES_SLICE, POSTGRES, TAILSCALED)

# The cgroups whose reservation must survive every ancestor, as paths under
# /sys/fs/cgroup. A unit here is only as protected as the least generous slice
# on its path. Postgres instances are discovered live — see _postgres_cgroups.
PROTECTED_CGROUPS = (
    Path(SYSTEM_SLICE) / WATCHER,
    Path(SYSTEM_SLICE) / TAILSCALED,
)


def _dropin(unit: str) -> Path:
    return DROPINS / f"{unit}.d" / DROPIN_NAME


def _text(unit: str) -> str:
    return _dropin(unit).read_text()


def _memory_low(unit: str) -> int:
    """Return the drop-in's single ``MemoryLow=`` in bytes.

    Strict on purpose: systemd does not strip a trailing ``# comment`` from a
    value, so ``MemoryLow=384M  # margin`` fails to parse and the directive is
    ignored with a log line nobody reads. ``memory_size_to_bytes`` raises on
    the same input.
    """
    (value,) = directive_values(_text(unit), "MemoryLow")
    return memory_size_to_bytes(value)


def _watcher_memory_low() -> int:
    (value,) = directive_values(WATCHER_UNIT.read_text(), "MemoryLow")
    return memory_size_to_bytes(value)


@pytest.mark.parametrize("unit", TARGETS)
def test_dropin_is_a_reservation_not_a_cap(unit: str) -> None:
    """Each drop-in reserves memory and caps nothing (#307's rule, extended).

    A cap on a dependency stalls it while it still reports ``active`` — for
    postgres that is every watcher request hanging on a throttled database.
    """
    text = _text(unit)
    assert _memory_low(unit) > 0, f"{unit}: MemoryLow reserves nothing"
    for directive in ("MemoryHigh", "MemoryMax", "MemoryMin"):
        assert not directive_values(text, directive), f"{unit}: sets {directive}="


@pytest.mark.parametrize("unit", TARGETS)
def test_dropin_uses_the_section_its_unit_type_reads(unit: str) -> None:
    """A slice reads ``[Slice]``, a service ``[Service]``.

    ``MemoryLow=`` under the wrong section header is ignored, not rejected —
    the drop-in installs, reloads, and reserves nothing.
    """
    section = "[Slice]" if unit.endswith(".slice") else "[Service]"
    headers = [line.strip() for line in _text(unit).splitlines() if line.strip().startswith("[")]
    assert headers == [section], f"{unit}: sections {headers}, expected [{section}]"


def test_system_slice_grants_what_its_children_claim() -> None:
    """The #309 blocker: a child keeps no more than its slice grants.

    ``system.slice`` must cover watcher (from ``deploy/watcher.service``), the
    postgres slice, and ``tailscaled``.
    """
    claimed = _watcher_memory_low() + _memory_low(POSTGRES_SLICE) + _memory_low(TAILSCALED)
    assert _memory_low(SYSTEM_SLICE) >= claimed, (
        f"system.slice grants {_memory_low(SYSTEM_SLICE)} bytes but its children claim {claimed}"
    )


def test_postgres_slice_grants_what_postgres_claims() -> None:
    """The templated unit sits one slice deeper, so it needs two grants."""
    assert _memory_low(POSTGRES_SLICE) >= _memory_low(POSTGRES)


def test_postgres_dropin_leaves_debians_oom_adjustment_alone() -> None:
    """Debian ships -900 for the postmaster; this drop-in must not override it.

    Backends reset themselves to 0 by design (a killed backend costs a crash
    recovery, a killed postmaster costs the database) — that is Debian's call,
    and this file does not second-guess it.
    """
    assert not directive_values(_text(POSTGRES), "OOMScoreAdjust")


def test_tailscaled_ranks_between_watcher_and_the_default() -> None:
    """``tailscaled`` below the default 0, but behind watcher.

    At 0 it ranks level with every ``npm``/``node`` process on the box, and
    below them once they grow. Behind watcher by design: the dashboard is
    reached through the exe.dev proxy, not the tailnet, so it should outlive
    the tunnel.
    """
    (watcher,) = directive_values(WATCHER_UNIT.read_text(), "OOMScoreAdjust")
    (tailscaled,) = directive_values(_text(TAILSCALED), "OOMScoreAdjust")
    assert int(watcher) < int(tailscaled) < 0


def _on_host() -> bool:
    return (INSTALLED / WATCHER).exists()


@pytest.mark.parametrize("unit", TARGETS)
def test_installed_dropin_matches_repo(unit: str) -> None:
    """A rebuilt VM, or a hand edit, must not diverge silently."""
    if not _on_host():
        pytest.skip(f"{INSTALLED / WATCHER} not present — not a host running the service")
    installed = INSTALLED / f"{unit}.d" / DROPIN_NAME
    assert installed.exists(), (
        f"{installed} is missing.\nInstall with:\n"
        f"  sudo install -D -m 644 {_dropin(unit)} {installed} && sudo systemctl daemon-reload"
    )
    assert installed.read_text() == _text(unit), (
        f"{installed} has drifted from {_dropin(unit)}.\nReinstall with:\n"
        f"  sudo install -D -m 644 {_dropin(unit)} {installed} && sudo systemctl daemon-reload"
    )


def _cgroup_memory_low(path: Path) -> int:
    raw = (CGROUP_ROOT / path / "memory.low").read_text().strip()
    return 2**63 if raw == "max" else int(raw)


@pytest.mark.parametrize("cgroup", PROTECTED_CGROUPS, ids=str)
def test_live_reservation_survives_every_ancestor(cgroup: Path) -> None:
    """Read the kernel, not ``systemctl show`` — the file is not the effect.

    ``systemctl show`` reported watcher's 512M for #307's whole life while the
    kernel granted 0. At every level from the unit up to ``system.slice``, the
    parent's ``memory.low`` must cover the sum of its children's.

    Gated on the host, not on the cgroup: a laptop that happens to run
    ``tailscaled`` is not this host, and on this host a missing unit is a
    finding, not a reason to skip.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED / WATCHER} not present — not a host running the service")
    assert (CGROUP_ROOT / cgroup).is_dir(), f"{CGROUP_ROOT / cgroup} missing — is the unit up?"
    _assert_reservation_survives_every_ancestor(cgroup)


def _postgres_cgroups() -> list[Path]:
    """Every running ``postgresql@<cluster>.service`` cgroup, as a relative path."""
    slice_dir = CGROUP_ROOT / SYSTEM_SLICE / POSTGRES_SLICE
    if not slice_dir.is_dir():
        return []
    return sorted(p.relative_to(CGROUP_ROOT) for p in slice_dir.glob("postgresql@*.service"))


def test_live_postgres_instances_are_all_reserved() -> None:
    """Every running cluster carries the template's reservation.

    Discovered rather than named: a test pinned to ``postgresql@16-main`` skips
    once a major upgrade renames the unit, and reads green while the new cluster
    runs unreserved. On the host, no running instance at all is a failure.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED / WATCHER} not present — not a host running the service")
    instances = _postgres_cgroups()
    assert instances, f"no postgresql@*.service cgroup under {POSTGRES_SLICE} — is postgres up?"
    for cgroup in instances:
        _assert_reservation_survives_every_ancestor(cgroup)


def _assert_reservation_survives_every_ancestor(cgroup: Path) -> None:
    assert _cgroup_memory_low(cgroup) > 0, f"{cgroup} has no live memory.low"
    node = cgroup
    while node.parent != Path("."):
        parent = node.parent
        children = [c for c in (CGROUP_ROOT / parent).iterdir() if (c / "memory.low").exists()]
        claimed = sum(_cgroup_memory_low(c.relative_to(CGROUP_ROOT)) for c in children)
        granted = _cgroup_memory_low(parent)
        assert granted >= claimed, (
            f"{parent} grants {granted} bytes but its children claim {claimed} — "
            f"{cgroup} keeps at most what {parent} grants"
        )
        node = parent


def _live_oom_score_adj(unit: str) -> int:
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
        pytest.skip(f"{unit} is not running")
    return int(Path(f"/proc/{pid}/oom_score_adj").read_text())


def test_live_oom_order() -> None:
    """``OOMScoreAdjust=`` applies at exec, so check the process, not the unit.

    After a ``daemon-reload`` the unit reports the new value while the running
    process keeps the old one until it restarts. Order: postmaster < watcher <
    tailscaled < the default 0.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED / WATCHER} not present — not a host running the service")
    instances = _postgres_cgroups()
    assert instances, f"no postgresql@*.service cgroup under {POSTGRES_SLICE} — is postgres up?"
    watcher = _live_oom_score_adj(WATCHER)
    tailscaled = _live_oom_score_adj(TAILSCALED)
    for cgroup in instances:
        postgres = _live_oom_score_adj(cgroup.name)
        assert postgres < watcher < tailscaled < 0, (
            f"{cgroup.name} postmaster {postgres}, watcher {watcher}, tailscaled {tailscaled}"
        )
