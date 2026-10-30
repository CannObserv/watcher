"""ci.yml and audit.yml pin their runner image on purpose (#365).

**``ubuntu-24.04``, not ``ubuntu-latest``: CI matches the host's userland.**
``ubuntu-latest`` moved to 26.04 on GitHub's schedule (actions/runner-images#14748).
Its first run on 26.04, 41007187278, failed ``integration``: the image's
PostgreSQL 18 ``pg_dump`` writes ``SET transaction_timeout`` into the archive, and
the ``postgres:16`` service rejects it on restore. co-watcher is 24.04, with a
16.15 client against a 16.15 server; the 24.04 image ships the same client, so
the backup rehearsal runs the pair production runs.

**The removal trigger is this file.** When the host moves, CI moves with it: on
the host, ``test_runner_image_matches_the_host`` fails until the pin follows.
Off the host it skips, so it sits in the ``test`` job's standing skip set.

``context-cadence.yml`` stays on ``ubuntu-latest``: it is generated from
curating-context's template, runs nothing the host runs, and is outside the gate.

Ported from CannObserv/archiver#192 and CannObserv/broker#88. Pure file reads,
plus ``/etc/os-release`` on the host.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
#: The workflows the host's userland governs; context-cadence.yml is not one.
PINNED = (WORKFLOWS / "ci.yml", WORKFLOWS / "audit.yml")
OS_RELEASE = Path("/etc/os-release")
INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")


def _on_host() -> bool:
    return INSTALLED_UNIT.exists()


def _runners() -> set[str]:
    return {
        job.get("runs-on")
        for workflow in PINNED
        for job in yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"].values()
    }


def _host_image(os_release: Path) -> str:
    """The runner label for the distro ``os_release`` names: ``ubuntu-<VERSION_ID>``."""
    found = re.search(r'^VERSION_ID="?([^"\n]+)"?$', os_release.read_text(), re.MULTILINE)
    assert found, f"{os_release} names no VERSION_ID"
    return f"ubuntu-{found[1]}"


def test_ci_and_audit_run_on_one_pinned_image() -> None:
    runners = _runners()
    assert len(runners) == 1, f"one image for every job, found {runners}"
    (runner,) = runners
    assert not runner.endswith("-latest"), f"{runner} moves on GitHub's schedule, not the host's"


def test_runner_image_matches_the_host() -> None:
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present: not a host running the service")
    host = _host_image(OS_RELEASE)
    assert _runners() == {host}, (
        f"the host is {host}, CI runs {_runners()}: move every runs-on in "
        f"{', '.join(p.name for p in PINNED)} to {host} (#365)"
    )


@pytest.mark.parametrize(
    ("os_release", "image"),
    [
        ('NAME="Ubuntu"\nVERSION_ID="24.04"\n', "ubuntu-24.04"),
        ("VERSION_ID=26.04\n", "ubuntu-26.04"),
    ],
)
def test_host_image_reads_a_quoted_and_a_bare_value(
    tmp_path: Path, os_release: str, image: str
) -> None:
    path = tmp_path / "os-release"
    path.write_text(os_release)
    assert _host_image(path) == image


def test_a_host_that_moves_no_longer_matches(tmp_path: Path) -> None:
    """The pretend host: the check the real one runs, against a 26.04 os-release."""
    moved = tmp_path / "os-release"
    moved.write_text('VERSION_ID="26.04"\n')
    assert _runners() != {_host_image(moved)}
