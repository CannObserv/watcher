"""needrestart lists restarts and never performs them (#331).

apt's ``DPkg::Post-Invoke`` hook (``/etc/apt/apt.conf.d/99needrestart``) runs
``needrestart -m u`` after every dpkg run. Ubuntu's patch turns ``-m u`` into
**automatic** restarts when ``$nrconf{restart}`` is unset — the stock state —
so a ``libc6`` security update would restart ``watcher`` and PostgreSQL
mid-apply, outside any window: the single process that runs the API, the
Procrastinate worker and both bus consumers, and the cluster under all three
databases. The drop-in makes the answer independent of whether
``NEEDRESTART_MODE=l`` survives every process between the operator and the
hook — though a ``NEEDRESTART_MODE`` or ``-r`` that *does* arrive still wins over
the config. Shape from CannObserv/notifier#91 and CannObserv/broker#65.

It governs needrestart's hook only, not maintainer scripts: ``postgresql-16``
restarts its own cluster on upgrade whatever this file says.

Tracked in ``deploy/``, installed as:

- ``needrestart.conf.d/watcher.conf`` -> ``/etc/needrestart/conf.d/``

Pure assertions on the tracked copy run everywhere; installed-parity and live
assertions are gated on the host running ``watcher.service``, like the other
drift checks in this package, and skip elsewhere, CI included. On this host a
missing drop-in is a finding, not a reason to skip.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DROPIN = REPO_ROOT / "deploy" / "needrestart.conf.d" / "watcher.conf"
INSTALLED = Path("/etc/needrestart/conf.d/watcher.conf")
MAIN_CONF = Path("/etc/needrestart/needrestart.conf")
INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")
INSTALL_HINT = f"Install with:\n  sudo install -D -m 644 {DROPIN} {INSTALLED}"

# needrestart's config is Perl, eval'd into `%nrconf`. Evaluating it the same
# way is the only honest parse: a syntax error makes needrestart die, and the
# apt hook swallows that with `|| true`.
_EVAL = (
    "our %nrconf; our $LOGPREF = q(); "
    "eval do { local(@ARGV, $/) = $ARGV[0]; <> }; die $@ if $@; "
    "print defined $nrconf{restart} ? $nrconf{restart} : q(undef);"
)


def _restart_mode(conf: Path) -> str:
    """Evaluate ``conf`` as needrestart does and return ``$nrconf{restart}``."""
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl not available on this host")
    result = subprocess.run(
        [perl, "-e", _EVAL, str(conf)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"perl failed to evaluate {conf}:\n{result.stderr}"
    return result.stdout


def _on_host() -> bool:
    return INSTALLED_UNIT.exists()


def test_dropin_sets_list_only_restart_mode() -> None:
    """The tracked copy parses as Perl and sets ``restart`` to ``l``."""
    assert _restart_mode(DROPIN) == "l"


def test_dropin_sets_nothing_else() -> None:
    """One key, so the drop-in cannot quietly change needrestart's other
    behaviour, and ``$nrconf{ui}`` stays unset — setting it would force
    interactive mode instead."""
    lines = [
        ln.strip()
        for ln in DROPIN.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines == ["$nrconf{restart} = 'l';"]


def test_installed_copy_matches_tracked() -> None:
    """The installed drop-in is byte-identical to the tracked one.

    Gated on the host, not on the drop-in: gating on the file under test would
    pass silently on exactly the host that never received it.
    """
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    assert INSTALLED.exists(), f"{INSTALLED} is missing.\n{INSTALL_HINT}"
    assert INSTALLED.read_text() == DROPIN.read_text(), (
        f"{INSTALLED} has drifted from {DROPIN}.\n{INSTALL_HINT}"
    )


def test_live_config_chain_resolves_to_list_only() -> None:
    """The main config globs ``conf.d/*.conf`` in sort order, so a later file
    could override this one. Evaluate the chain needrestart itself reads —
    on the host running the service, where the stock chain leaving the key
    unset is the state this file exists to change."""
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    if not MAIN_CONF.exists():
        pytest.skip("needrestart not installed on this host")
    assert _restart_mode(MAIN_CONF) == "l", f"{INSTALLED} not in effect.\n{INSTALL_HINT}"
