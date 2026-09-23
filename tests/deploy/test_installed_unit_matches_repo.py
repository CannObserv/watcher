"""The installed systemd unit must match the repo copy.

Ported from archiver (#233). ``deploy/watcher.service`` gained
``Environment=WATCHER_ALLOW_PRODUCTION_DB=1``, without which the service
refuses to start once ``src.core.db_safety`` is deployed. That unit is
installed to ``/etc/systemd/system/`` by hand, and nothing verified it stayed
in sync afterwards.

Which is the same failure class as the incident that started this workstream:
the deployed thing quietly diverging from the documented thing. A future edit
to ``deploy/watcher.service`` that never reaches the VM would either fail to
apply a needed setting or, worse, leave the service unable to start on its
next restart.

Skips when the unit is not installed, so CI and dev clones pass; it only
asserts on a host that actually runs the service.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_UNIT = Path(__file__).resolve().parents[2] / "deploy" / "watcher.service"
INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")


def _read_if_installed(path: Path) -> str | None:
    """Return the unit's text, or None when it is genuinely not installed.

    Only ``FileNotFoundError`` means "not installed". A ``PermissionError`` is
    deliberately allowed to propagate: swallowing it would turn an unreadable
    unit into a silent pass, and a drift check that cannot fail is worse than
    no check at all — it reads as coverage while asserting nothing.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _unset_environment(unit_text: str) -> set[str]:
    """Return every variable named by an ``UnsetEnvironment=`` line.

    ``UnsetEnvironment=`` takes a space-separated list and may be repeated, so
    the directive's meaning is the union of its tokens — not the text of any
    one line. Parsing keeps the assertions in this module indifferent to how a
    future edit chooses to group the names.
    """
    names: set[str] = set()
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("UnsetEnvironment="):
            names.update(stripped.split("=", 1)[1].split())
    return names


def _directive_values(unit_text: str, directive: str) -> list[str]:
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


def test_repo_unit_loads_no_env_file_from_the_checkout() -> None:
    """#296 D5: the service must not inherit the workstation's secrets.

    The checkout's ``.env`` is where the agent workspace keeps its credentials —
    ``ANTHROPIC_API_KEY`` and a ``GH_TOKEN_*`` per sibling repo — and a
    production unit that loads it hands all of them to the process serving the
    dashboard. Nothing in it is production configuration: the service's own
    settings live in ``/etc/watcher/.env``.

    The move to a dedicated VM does not fix this on its own. The dev workspace
    moves with the service (replicator#88 D2), so the new checkout's ``.env``
    carries the same secrets again; only the unit can stop reading it.

    Asserted against the unit's ``WorkingDirectory=`` rather than a literal
    ``.env`` name, so a backup or an alternate file beside it is caught too.
    """
    text = REPO_UNIT.read_text()
    (checkout,) = _directive_values(text, "WorkingDirectory")
    under_checkout = [
        path
        for path in (
            # The optional-file prefix, dropped here and nowhere else: an
            # optional env file under the checkout is exactly as loaded as a
            # required one whenever it exists.
            value.removeprefix("-")
            for value in _directive_values(text, "EnvironmentFile")
        )
        if Path(path).is_relative_to(checkout)
    ]
    assert not under_checkout, f"unit loads env files from the checkout: {under_checkout}"


def test_repo_unit_orders_after_tailscaled_without_depending_on_it() -> None:
    """#296: start after the tailnet agent, but never be bound to it.

    Every peer this service talks to — the broker, notifier — is a MagicDNS name
    on the tailnet, so starting before ``tailscaled`` only guarantees a burst of
    connection failures. Ordering is all that is wanted, and it is all that is
    safe: ``Requires=``/``BindsTo=`` would stop the dashboard whenever a
    Tailscale upgrade restarts the agent, and ``Wants=`` would make this unit
    responsible for starting a daemon it does not own. replicator#88 pins the
    same shape.

    Ordering does not close the race on its own — the name answers after the
    address (replicator#88) — which is what ``probe_bus_reachable``'s window is
    for. This test pins the half systemd can do.
    """
    text = REPO_UNIT.read_text()
    after = " ".join(_directive_values(text, "After")).split()
    assert "tailscaled.service" in after
    for directive in ("Wants", "Requires", "BindsTo", "Requisite", "PartOf"):
        bound = " ".join(_directive_values(text, directive)).split()
        assert "tailscaled.service" not in bound, f"{directive}= must not name tailscaled"


def test_repo_unit_declares_the_production_opt_in() -> None:
    """The opt-in must live in the unit, never in an env file.

    An EnvironmentFile is sourced by every process that loads it — putting the
    flag there would re-open the hole for hand-run servers, which is exactly
    what the guard exists to close.
    """
    text = REPO_UNIT.read_text()
    assert "Environment=WATCHER_ALLOW_PRODUCTION_DB=1" in text


def test_repo_unit_drops_the_migration_credential() -> None:
    """#270: the service process must not inherit the DDL credential.

    ``WATCHER_MIGRATION_DATABASE_URL`` lives in ``/etc/watcher/.env`` beside
    ``DATABASE_URL``, and the unit loads that file wholesale — so the service
    inherited the one credential that can drop tables even though the
    connection it opens is the DML-only ``watcher_app`` one (#259). Only
    ``alembic/env.py`` ever reads it; nothing in the running service does, and
    ``alembic`` run from a shell still resolves it from the env file.

    ``UnsetEnvironment=``, not ``Environment=WATCHER_MIGRATION_DATABASE_URL=``;
    the unit comment on that line explains why blanking cannot work.

    Asserted on the parsed token rather than the whole line, because
    ``UnsetEnvironment=`` takes a space-separated list: a second variable added
    later is valid systemd and must not read as a regression here.
    """
    assert "WATCHER_MIGRATION_DATABASE_URL" in _unset_environment(REPO_UNIT.read_text())


def test_repo_unit_declares_the_bus_opt_in() -> None:
    """#262: the bus gate is unit-only, for the same reason as the DB one.

    ``WATCHER_BUS_REDIS_URL`` lives in ``/etc/watcher/.env``, so every process
    that sources it inherits a broker address. The flag is what separates the
    service from an agent shell or a REPL, and it only works if no env file
    carries it. Without this line the service starts and refuses to publish —
    which is why ``src/core/bus.py`` also makes the URL-without-flag
    combination a startup failure rather than a silent one.
    """
    text = REPO_UNIT.read_text()
    assert "Environment=WATCHER_BUS_ENABLED=1" in text


def test_repo_unit_declares_the_notifier_opt_in() -> None:
    """#277: the notifier gate is unit-only, for the same reason as the other two.

    ``WATCHER_NOTIFIER_BASE_URL`` and ``WATCHER_NOTIFIER_API_KEY`` lived in
    ``/etc/watcher/.env`` when this flag was added, so every process that
    sourced it inherited the production tenant's credentials. The flag is what
    separated the service from a pytest run, a hand-run dev server, a script or
    a REPL — and it only works if no env file carries it. #278 moved the pair
    to ``/etc/watcher/notifier.env`` so nothing but the unit holds it either;
    the flag stays, because it is what the app checks
    (``tests/deploy/test_notifier_credential_is_unit_only.py`` owns the move).

    Without this line the service starts and refuses to notify,
    which is why ``src/core/notifier_client`` also makes the URL-without-flag
    combination a startup failure rather than a silent one.
    """
    text = REPO_UNIT.read_text()
    assert "Environment=WATCHER_NOTIFIER_ENABLED=1" in text


def test_repo_unit_treats_sigterm_exit_as_success() -> None:
    """#256: a graceful stop must not read as a failure.

    uvicorn exits 143 (128+15) on SIGTERM, which is the normal path for
    ``systemctl stop``. Without ``SuccessExitStatus=143`` systemd files that
    under ``failed``, so ``systemctl is-active watcher`` — the one signal an
    operator checks first — cannot tell a routine stop from a crash, and the
    journal has to be read to find out which happened.
    """
    text = REPO_UNIT.read_text()
    assert "SuccessExitStatus=143" in text


def test_systemd_has_loaded_the_installed_unit() -> None:
    """A copied unit that was never ``daemon-reload``ed is still not in force.

    ``test_installed_unit_matches_repo`` compares bytes on disk. systemd serves
    the unit it parsed at the last reload, so ``sudo cp`` alone leaves every
    other assertion in this module green while the running service keeps its
    old configuration.

    #270 is what made that gap worth closing: ``WATCHER_ALLOW_PRODUCTION_DB``
    and ``WATCHER_BUS_ENABLED`` announce a missed reload the next time the
    service restarts — it fails to start, or refuses to publish.
    ``UnsetEnvironment=`` announces nothing. The credential is simply still
    there, and the only signal is this one.

    Skips off-host for the same reason the drift check does; ``NeedDaemonReload``
    is readable without privileges, so nothing here needs root.
    """
    if _read_if_installed(INSTALLED_UNIT) is None:
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        pytest.skip("systemctl not available")

    result = subprocess.run(
        [systemctl, "show", "watcher", "--property=NeedDaemonReload", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"systemctl show failed: {result.stderr.strip()}")

    assert result.stdout.strip() == "no", (
        "systemd has not reloaded the installed unit — the file at "
        f"{INSTALLED_UNIT} is not what the service is running.\n"
        "Reload with:\n"
        "  sudo systemctl daemon-reload\n"
        "Then restart the service when it is safe to do so."
    )


def test_installed_unit_matches_repo() -> None:
    installed = _read_if_installed(INSTALLED_UNIT)
    if installed is None:
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    assert installed == REPO_UNIT.read_text(), (
        f"{INSTALLED_UNIT} has drifted from {REPO_UNIT}.\n"
        "Reinstall with:\n"
        f"  sudo cp {REPO_UNIT} {INSTALLED_UNIT} && sudo systemctl daemon-reload\n"
        "Then restart the service when it is safe to do so."
    )


def _memory_size_to_bytes(value: str) -> int:
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


def test_repo_unit_reserves_memory_against_a_co_tenant_session() -> None:
    """#307: the service gets the reservation, because the session cannot be killed.

    Dev and prod share this VM (AGENTS.md → Infrastructure), and exe.dev session
    processes inherit ``oom_score_adj`` **-1000** from ``exe-init`` and ``sshd``.
    The OOM killer therefore can never pick the agent session that is spiking —
    it picks the host's production service instead. That is what happened on
    CannObserv/broker's VM on 2026-09-16: nothing that spiked was killed, the
    kernel failed atomic allocations in ``tailscaled`` and ``ksoftirqd``, and the
    bus was down 57m 48s.

    A cap on the session is not a substitute. This host has no swap, so the
    reservation is the only directive that keeps this service's working set out
    of reclaim while something else on the box is growing — and only while
    ``system.slice`` grants it (#309, ``test_memory_dropins.py``).
    """
    text = REPO_UNIT.read_text()
    values = _directive_values(text, "MemoryLow")
    assert values, "unit declares no MemoryLow= reservation"
    (reserved,) = values
    assert _memory_size_to_bytes(reserved) > 0, f"MemoryLow={reserved} reserves nothing"


def test_repo_unit_takes_no_throttling_cap() -> None:
    """#307: a reservation, never a cap — a cap on this process stalls it.

    ``MemoryHigh=`` throttles reclaim rather than failing an allocation, so a
    production unit that hits it does not crash and restart: it slows to a
    crawl while still reporting ``active``, which is strictly worse for a
    dashboard than an honest failure. ``MemoryMax=`` is the same trade with a
    kill at the end.

    The cap belongs on the *install* that spikes — the ``systemd-run --scope``
    around the pinned SocratiCode pre-install — not on the service being
    protected from it.
    """
    text = REPO_UNIT.read_text()
    for directive in ("MemoryHigh", "MemoryMax"):
        for value in _directive_values(text, directive):
            assert value == "infinity", f"{directive}={value} throttles the service"


def test_repo_unit_lowers_its_oom_score() -> None:
    """#307: make the killer prefer anything else on the box.

    Everything else here runs at the default 0 — an ``npm install``, a `node`
    server, a docker build — while the agent session sits at -1000 and is
    unpickable. A negative score moves this service behind all of them.

    Deliberately not -1000: an unkillable service on a host with no swap
    means the kernel runs out of candidates and wedges the box instead of
    shedding one process.
    """
    text = REPO_UNIT.read_text()
    values = _directive_values(text, "OOMScoreAdjust")
    assert values, "unit declares no OOMScoreAdjust="
    (adjust,) = values
    assert -1000 < int(adjust) < 0, f"OOMScoreAdjust={adjust} must be negative but not -1000"
