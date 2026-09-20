"""What `scripts/cleanup.sh` has to keep once the Docker branch is gone (#310).

The script runs under `set -euo pipefail`, so a non-zero exit anywhere in it
ends the run: every guard in it is load-bearing for every step *below* it, and
the weekly timer reports only the unit's exit status, so an abort reads as a run
that simply ended. That assertion arrived here from
`tests/deploy/test_cleanup_docker_guard.py`, which #300 wrote around the Docker
branch and #310 deleted with it — it was always about the script, not about
Docker.

The deleted file's premise is now pinned in reverse. #300 moved the semantic
index to the cohort's shared store on `co-index` and #310 purged the packages
from this host, so a `docker` call here would no longer be a branch that skips
itself on a daemon that is down — it would be `command not found`, aborting the
weekly run at that line and silently skipping the journal vacuum below it.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLEANUP = REPO_ROOT / "scripts" / "cleanup.sh"


def test_pipefail_is_what_makes_every_guard_load_bearing():
    """The premise of this whole file. If it ever goes, re-derive the rest."""
    assert "set -euo pipefail" in CLEANUP.read_text()


def test_the_script_never_calls_docker():
    """No binary on this host answers it, and `set -e` makes that fatal (#310).

    Checked over the whole file, comments included: a comment describing a
    branch that no longer exists is the thing #310 removed, not a survivor.
    """
    assert "docker" not in CLEANUP.read_text().lower(), (
        "scripts/cleanup.sh names Docker again — #310 purged the packages from "
        "this host, so the call is `command not found` and aborts the run"
    )


def test_the_journal_vacuum_still_runs():
    """The step the removed guard existed to protect — pinned to outlive it."""
    assert "--vacuum-time=14d" in CLEANUP.read_text()
