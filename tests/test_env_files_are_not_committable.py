"""An env file, or anything shaped like one, must be unstageable (#280).

``.gitignore`` listed ``.env`` exactly. The notifier VM cutover repointed the
file in place and left ``.env.bak-precutover-*`` beside it — a byte-for-byte
copy holding every token the real file holds, matching no pattern, and showing
up as an untracked file one ``git add -A`` away from being published.

This is the third guard on the same boundary and the only one that acts before
the fact. ``tests/deploy/test_notifier_credential_is_unit_only.py`` asserts
that nothing *shell-readable* holds the production notifier pair, and
``scripts/load-env.sh`` decides what a shell inherits; both operate on files
that already exist. Git's ignore rules are what stop a copy of one from
reaching a remote, where deletion does not undo publication.

The two guards share ``ENV_FILE_PREFIXES`` deliberately: a filename family the
credential sweep knows to *read* is one git must know to *ignore*, and letting
those lists drift is how ``notifier.env.bak`` ended up detectable but
committable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy.test_notifier_credential_is_unit_only import ENV_FILE_PREFIXES

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Git answers every question here, so a tree without it has nothing to assert
#: rather than a failure to report (CR-13) — the same shape as the on-host
#: assertions in ``tests/deploy/``, which skip off the VM.
pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists() or shutil.which("git") is None,
    reason="not a git checkout, or git is unavailable",
)

#: Every family in ``ENV_FILE_PREFIXES``, several names deep. One member per
#: family is what CR-11 caught: ``.env.*`` needs a literal dot, so ``.envrc``
#: read as a credential candidate while git was willing to publish it, and a
#: single-representative assertion certified the lockstep anyway. Names only —
#: none of these are created; ``--no-index`` means git answers about the path,
#: not about a file on disk.
BACKUP_NAMES = (
    ".env",
    ".envrc",
    ".env.bak",
    ".env.local",
    ".env.bak-precutover-20260828T183354Z",
    "notifier.env",
    "notifier.env.bak",
    "notifier.env.save",
)

#: The negative control. Without it a rule of ``*`` would pass every assertion
#: above and this module would be measuring nothing.
TRACKED_NAMES = ("pyproject.toml", "docs/ENVIRONMENT.md", "scripts/load-env.sh")


def _is_ignored(name: str) -> bool:
    """Whether git's ignore rules would exclude ``name`` in this repo.

    ``--no-index`` so the answer is about the patterns rather than about what
    happens to be tracked today: a file already in the index reports as
    non-ignored, which would make a real gap look closed.

    ``check-ignore`` answers with 0 (ignored) or 1 (not ignored) and reserves
    128/129 for its own failures. Reading "non-zero" as "not ignored" (CR-13)
    turns a git that cannot run — an unsupported flag, an exported tree with no
    repository — into a report that this repo's ignore rules are broken, which
    sends the reader after a policy bug that does not exist.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", name],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"git check-ignore failed on {name!r} (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.returncode == 0


def test_a_git_failure_is_not_read_as_not_ignored(monkeypatch) -> None:
    """CR-13: the error exits must not masquerade as a policy verdict."""

    class _Failed:
        returncode = 129
        stderr = b"error: unknown option `bogus'"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Failed())
    with pytest.raises(RuntimeError, match="exit 129"):
        _is_ignored(".env")


@pytest.mark.parametrize("name", BACKUP_NAMES)
def test_env_shaped_files_are_ignored(name: str) -> None:
    assert _is_ignored(name), (
        f"{name} is not ignored — a copy of a secrets file is a secrets file, "
        "and this one can be staged. Add its family to .gitignore."
    )


@pytest.mark.parametrize("name", TRACKED_NAMES)
def test_ordinary_repo_files_are_not_ignored(name: str) -> None:
    """Proves the assertions above are testing a rule, not a blanket."""
    assert not _is_ignored(name)


def test_every_credential_prefix_family_is_ignored() -> None:
    """The lockstep the module docstring describes, asserted rather than assumed.

    ``_repo_env_candidates`` reads any repo-root file starting with one of
    these. Every such name must also be unstageable, or the sweep detects a
    credential in a file git was willing to publish.
    """
    unignored = [prefix for prefix in ENV_FILE_PREFIXES if not _is_ignored(f"{prefix}.bak")]
    assert not unignored, (
        "these credential-sweep prefixes are readable by the sweep but not "
        f"ignored by git: {', '.join(unignored)}"
    )
