"""Tests for ``scripts/audit_lock.sh``: the lock audit reports and never fails (#397).

The script runs ``uv audit --frozen`` against ``uv.lock`` twice: the runtime
packages (``--no-dev``) and the dev group (``--only-dev``), reported apart so a
standing dev advisory never masks a new runtime one.
``.github/workflows/audit.yml`` runs it weekly and on a lock-touching push or PR
(``tests/ci/test_audit_workflow.py``). Ported from CannObserv/broker#86 and
CannObserv/archiver#420.

**Report-only is the contract.** A gate that failed on a finding would turn
``main`` red on someone else's disclosure date. So every exit is 0. A finding
becomes a warning annotation plus the job summary, and an audit that could not
run gets its own title, so it never reads as clean.

The script runs here against a fake ``uv`` on ``PATH``, so nothing reaches OSV.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_lock.sh"

# What the script runs, in order: (group flag, warning label).
AUDITS = [("--no-dev", "uv.lock runtime"), ("--only-dev", "uv.lock dev")]
FLAG = ["--preview-features", "audit-command"]

# uv < 0.11.19 has no ``audit-command`` name (astral-sh/uv#19645): its parser
# rejects the value before the command runs.
UNKNOWN_FLAG = (
    "error: invalid value 'audit-command' for '--preview-features <PREVIEW_FEATURES>': "
    "Unknown feature flag\n"
)


def _argv(group: str, *, flag: bool = True) -> list[str]:
    return ["audit", "--frozen", group, *(FLAG if flag else [])]


def _run(
    tmp_path: Path,
    uv_exit: int,
    uv_output: str = "",
    *,
    actions: bool = True,
    knows_flag: bool = True,
    per_group: dict[str, tuple[int, str]] | None = None,
    argv0: str | None = None,
):
    """Run the script with a fake ``uv`` that records each call's argv, prints
    ``uv_output`` and exits ``uv_exit``; ``per_group`` overrides both for one
    group flag. With ``knows_flag=False`` it answers ``--preview-features`` as
    uv < 0.11.19 does, exit 2, and runs nothing. ``argv0`` runs the script's
    text under that ``$0`` instead of its path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    responses = tmp_path / "responses"
    responses.mkdir()
    argv_file = tmp_path / "argv"
    (responses / "default.out").write_text(uv_output)
    (responses / "default.exit").write_text(str(uv_exit))
    for group, (code, text) in (per_group or {}).items():
        (responses / f"{group}.out").write_text(text)
        (responses / f"{group}.exit").write_text(str(code))
    (tmp_path / "unknown").write_text(UNKNOWN_FLAG)
    reject = (
        ""
        if knows_flag
        else (
            'if [[ " $* " == *" --preview-features "* ]]; then\n'
            f'\tcat "{tmp_path / "unknown"}" >&2\n'
            "\texit 2\n"
            "fi\n"
        )
    )
    fake = bin_dir / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{argv_file}"\n'
        f"{reject}"
        "group=\n"
        'for a in "$@"; do\n'
        "\tcase $a in --no-dev|--only-dev) group=$a ;; esac\n"
        "done\n"
        f'r="{responses}/$group"\n'
        f'[ -f "$r.exit" ] || r="{responses}/default"\n'
        'cat "$r.out"\n'
        'exit "$(cat "$r.exit")"\n'
    )
    fake.chmod(0o755)
    summary = tmp_path / "summary.md"
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    if actions:
        env |= {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}
    argv = ["bash", str(SCRIPT)] if argv0 is None else ["bash", "-c", SCRIPT.read_text(), argv0]
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    calls = (
        [line.split() for line in argv_file.read_text().splitlines()] if argv_file.exists() else []
    )
    return result, calls, summary.read_text() if summary.exists() else ""


@pytest.mark.parametrize("uv_exit", [0, 1, 2, 127])
def test_every_exit_is_zero(tmp_path, uv_exit):
    """Whatever uv says, the step stays green: report-only."""
    result, _, _ = _run(tmp_path, uv_exit)
    assert result.returncode == 0, result.stderr


def test_audits_runtime_then_dev_without_relocking(tmp_path):
    """``--frozen`` reads ``uv.lock`` as committed. A relock would need the
    private wheelhouse, which the job has no credential to fetch. ``uv audit``
    is a preview command, so the flag goes first and a uv that knows it runs
    once per group."""
    _, calls, _ = _run(tmp_path, 0)
    assert calls == [_argv(group) for group, _ in AUDITS]


def test_runtime_and_dev_are_reported_apart(tmp_path):
    """A standing dev advisory (virtualenv, pygments) must not read as a
    runtime one, nor hide one."""
    result, _, summary = _run(
        tmp_path,
        0,
        "Found no known vulnerabilities\n",
        per_group={"--only-dev": (1, "- GHSA-dev: virtualenv\n")},
    )
    titles = re.findall(r"^::warning title=([^:]+)::", result.stdout, re.MULTILINE)
    assert titles == ["uv.lock dev advisories"]
    for group, label in AUDITS:
        assert f"### {label}: uv audit --frozen {group}" in summary
    assert summary.index("Found no known vulnerabilities") < summary.index("GHSA-dev")


@pytest.mark.parametrize(
    ("uv_exit", "uv_output", "warning"),
    [
        (0, "Found no known vulnerabilities\n", None),
        (1, "- PYSEC-2026-4175: urllib3\n", "::warning title=uv.lock runtime advisories::"),
    ],
)
def test_a_uv_older_than_the_flag_still_audits(tmp_path, uv_exit, uv_output, warning):
    """uv < 0.11.19 rejects ``audit-command`` with exit 2 and "Unknown feature
    flag", so the script retries without the flag: the command is there, only
    the name is not. From 0.11.20 an unknown name only warns (0.13.5 measured,
    #397), so no later uv reaches this path. The rejected attempt never
    reaches the report."""
    result, calls, summary = _run(tmp_path, uv_exit, uv_output, knows_flag=False)
    assert calls == [
        argv for group, _ in AUDITS for argv in (_argv(group), _argv(group, flag=False))
    ]
    assert "did not run" not in result.stdout
    if warning:
        assert warning in result.stdout
    else:
        assert "::warning" not in result.stdout
    assert f"exit {uv_exit}" in summary
    assert uv_output.strip() in summary
    assert "Unknown feature flag" not in summary


def test_a_retry_that_fails_never_reads_as_clean(tmp_path):
    """On an old uv the flagless call can still fail, an outage at OSV say.
    The warning and the summary report that call, not the rejection."""
    result, calls, summary = _run(tmp_path, 2, "error: OSV unreachable\n", knows_flag=False)
    assert len(calls) == 2 * len(AUDITS)
    assert "::warning title=uv.lock runtime audit did not run::uv audit exited 2" in result.stdout
    assert "exit 2" in summary
    assert "OSV unreachable" in summary
    assert "Unknown feature flag" not in summary


def test_any_other_exit_2_is_not_retried(tmp_path):
    """Only the unknown-flag rejection retries. An outage at OSV, or a lock uv
    cannot parse, also exits 2, and a second call would only repeat it."""
    result, calls, _ = _run(tmp_path, 2, "error: OSV unreachable\n")
    assert len(calls) == len(AUDITS)
    for _, label in AUDITS:
        assert f"::warning title={label} audit did not run::" in result.stdout


def test_clean_lock_raises_no_warning(tmp_path):
    result, _, summary = _run(tmp_path, 0, "Found no known vulnerabilities\n")
    assert "::warning" not in result.stdout
    assert summary.count("Found no known vulnerabilities") == len(AUDITS)


def test_finding_is_a_warning_and_lands_in_the_summary(tmp_path):
    out = "- PYSEC-2026-4175: urllib3: HTTPS proxy TLS configuration\n"
    result, _, summary = _run(tmp_path, 1, out)
    for _, label in AUDITS:
        assert f"::warning title={label} advisories::" in result.stdout
    assert "PYSEC-2026-4175" in summary


@pytest.mark.parametrize("uv_exit", [2, 127])
def test_an_audit_that_did_not_run_never_reads_as_clean(tmp_path, uv_exit):
    """uv exits 1 for findings and 2 for an error: an outage at OSV, a lock it
    cannot parse, or a changed flag on an experimental command. A missing uv
    exits 127."""
    result, _, summary = _run(tmp_path, uv_exit)
    assert "::warning title=uv.lock runtime audit did not run::" in result.stdout
    assert f"exit {uv_exit}" in summary


def test_a_checkout_it_cannot_enter_is_not_audited(tmp_path):
    """The script audits the lock beside it, so it ``cd``\\s to the repo root
    first. If that fails it must not audit whatever lock the caller's directory
    holds, nor fail the step: it says the audit did not run (broker#112)."""
    result, calls, summary = _run(tmp_path, 0, argv0=str(tmp_path / "gone" / "scripts" / "x.sh"))
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert "::warning title=uv audit did not run::" in result.stdout
    assert summary == ""


def test_uv_output_cannot_issue_workflow_commands(tmp_path):
    """Advisory text comes from OSV. Echoed bare, a line starting ``::`` would be
    read by the runner as a workflow command, so each audit's output is echoed
    between ``stop-commands`` and that audit's resume token."""
    result, _, _ = _run(tmp_path, 1, "::error::forged\n")
    lines = result.stdout.splitlines()
    forged = [i for i, line in enumerate(lines) if line == "::error::forged"]
    assert len(forged) == len(AUDITS)
    for at in forged:
        stop = max(i for i in range(at) if lines[i].startswith("::stop-commands::"))
        token = lines[stop].removeprefix("::stop-commands::")
        assert len(token) >= 32
        assert lines.index(f"::{token}::", at) > at


def test_runs_off_actions_without_a_summary(tmp_path):
    """Run by hand, it prints each audit and touches no summary file."""
    result, _, summary = _run(tmp_path, 1, "a finding\n", actions=False)
    assert result.returncode == 0
    assert result.stdout.count("a finding") == len(AUDITS)
    assert "stop-commands" not in result.stdout
    assert summary == ""
