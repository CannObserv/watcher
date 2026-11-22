"""Pin the local-gate attestation's CI side and its knob (#456).

With ``.skills/attest-local-gate`` committed as ``on``, ``shipping-work``'s
``push.sh`` runs the gate on the exact tree, pushes only a pass, then posts the
commit status ``gate/local`` = ``success`` on the pushed SHA (the vendor's
``shipping-work/references/attestation.md``, gregoryfoster/skills#688). CI's
``test`` and ``integration`` jobs read it in an ``attest`` step and skip their
pytest steps for an attested human PR. ``lint`` and ``migrations`` always run:
the local gate builds no schema from empty and runs no ``alembic check``.

What is pinned, because each is a silent way to skip a tier that should run:

* **The skip's ``if:`` is the reference's, verbatim**, on both pytest steps and
  on ``integration``'s no-skip check (it reads the JUnit file the skipped step
  never writes). Evaluated here, it never skips for a bot's PR, a fork's PR, a
  tag, or any event but a push or a pull request.
* **The read fails loudly** and reads the PR's head, never the merge commit.
* **``statuses: read`` on each attesting job**, since a job's ``permissions``
  replace the workflow's (``tests/ci/test_co_pypi_ci.py`` holds the full sets).

Pure file reads, plus the vendored knob reader where the submodule is present.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
KNOB = REPO_ROOT / ".skills" / "attest-local-gate"
ATTEST_GATE_SH = REPO_ROOT / "skills" / "shipping-work" / "scripts" / "_attest-gate.sh"
ATTESTATION_MD = REPO_ROOT / "skills" / "shipping-work" / "references" / "attestation.md"
INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")

ATTESTING_JOBS = ("test", "integration")
ALWAYS_RUN_JOBS = ("lint", "migrations")

# attestation.md's `if:`, verbatim (gregoryfoster/skills be7c5809).
REFERENCE_IF = """
steps.attest.outputs.attested != 'yes'
|| (github.event_name != 'push' && github.event_name != 'pull_request')
|| startsWith(github.ref, 'refs/tags/')
|| (github.event_name == 'pull_request'
    && (github.event.pull_request.user.type == 'Bot'
        || github.event.pull_request.head.repo.full_name != github.repository))
"""

REPO = "CannObserv/watcher"


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _squash(expression: str) -> str:
    return " ".join(expression.split())


def _pytest_at(job: dict) -> int:
    found = [i for i, s in enumerate(job["steps"]) if "uv run pytest" in s.get("run", "")]
    assert len(found) == 1, f"want one pytest step, found {len(found)}"
    return found[0]


def _gated_steps(job: dict) -> list[dict]:
    return [s for s in job["steps"] if "steps.attest" in str(s.get("if", ""))]


# --- A minimal evaluator for the `if:` ----------------------------------------
# GitHub's expressions compare strings case-insensitively and read a missing
# property as null; both are modelled by lowering every string and reading a
# missing path as None. Anything outside the reference's grammar is a failure.

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<fn>startsWith\()|(?P<op>==|!=|&&|\|\||[(),])"
    r"|(?P<path>[A-Za-z_][\w.]*)(?![\w.])(?!\s*\())"
)


def _evaluate(expression: str, context: dict) -> bool:
    def lookup(path: str):
        node = context
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node.lower() if isinstance(node, str) else node

    def starts_with(text, prefix) -> bool:
        return (text or "").startswith(prefix or "")

    python, at, rest = [], 0, expression.strip()
    while at < len(rest):
        m = _TOKEN.match(rest, at)
        assert m and m.end() > at, f"outside the reference grammar at: {rest[at:]!r}"
        at = m.end()
        if m["str"]:
            python.append(repr(m["str"][1:-1].replace("''", "'").lower()))
        elif m["fn"]:
            python.append("starts_with(")
        elif m["op"]:
            python.append({"&&": " and ", "||": " or "}.get(m["op"], m["op"]))
        else:
            python.append(f"lookup({m['path']!r})")
    code = "".join(python)
    return bool(eval(code, {"__builtins__": {}}, {"lookup": lookup, "starts_with": starts_with}))


def _context(
    event: str,
    *,
    attested: str = "yes",
    ref: str | None = None,
    user_type: str = "User",
    head_repo: str = REPO,
) -> dict:
    ctx: dict = {
        "steps": {"attest": {"outputs": {"attested": attested}}},
        "github": {"event_name": event, "repository": REPO, "event": {}},
    }
    default_ref = "refs/pull/1/merge" if event == "pull_request" else "refs/heads/main"
    ctx["github"]["ref"] = ref or default_ref
    if event == "pull_request":
        ctx["github"]["event"]["pull_request"] = {
            "user": {"type": user_type},
            "head": {"repo": {"full_name": head_repo}},
        }
    return ctx


def _runs(context: dict) -> bool:
    """True when the heavy tier runs (the `if:` holds), False when it is skipped."""
    return _evaluate(REFERENCE_IF, context)


class TestTheSkipCondition:
    def test_an_attested_human_pr_skips(self) -> None:
        assert not _runs(_context("pull_request"))

    def test_an_attested_branch_push_skips(self) -> None:
        assert not _runs(_context("push"))

    @pytest.mark.parametrize("event", ["push", "pull_request"])
    @pytest.mark.parametrize("attested", ["no", "", "YES-ish"])
    def test_an_unattested_commit_runs(self, event: str, attested: str) -> None:
        assert _runs(_context(event, attested=attested))

    def test_a_missing_attest_output_runs(self) -> None:
        ctx = _context("pull_request")
        ctx["steps"] = {}
        assert _runs(ctx)

    def test_a_bots_pr_runs(self) -> None:
        assert _runs(_context("pull_request", user_type="Bot"))

    def test_a_forks_pr_runs(self) -> None:
        assert _runs(_context("pull_request", head_repo="someone/watcher"))

    def test_a_tag_runs(self) -> None:
        assert _runs(_context("push", ref="refs/tags/v1.0.0"))

    @pytest.mark.parametrize(
        "event", ["workflow_dispatch", "schedule", "pull_request_target", "merge_group"]
    )
    def test_any_other_event_runs(self, event: str) -> None:
        assert _runs(_context(event))

    def test_the_evaluator_refuses_what_it_does_not_model(self) -> None:
        with pytest.raises(AssertionError, match="outside the reference grammar"):
            _evaluate("contains(github.ref, 'x')", _context("push"))


class TestTheWorkflow:
    @pytest.mark.parametrize("name", ATTESTING_JOBS)
    def test_the_attest_step_sits_just_before_pytest(self, name: str) -> None:
        job = _jobs()[name]
        at = _pytest_at(job)
        assert job["steps"][at - 1].get("id") == "attest"
        assert [s.get("id") for s in job["steps"]].count("attest") == 1

    @pytest.mark.parametrize("name", ATTESTING_JOBS)
    def test_the_attest_step_reads_the_heads_combined_status_loudly(self, name: str) -> None:
        job = _jobs()[name]
        step = job["steps"][_pytest_at(job) - 1]
        assert step["env"] == {
            "GH_TOKEN": "${{ github.token }}",
            "HEAD": "${{ github.event.pull_request.head.sha || github.sha }}",
        }
        run = step["run"]
        assert "commits/$HEAD/status?per_page=100" in run
        assert 'select(.context == "gate/local" and .state == "success")' in run
        assert '>>"$GITHUB_OUTPUT"' in run
        assert "/statuses" not in run, "every status ever posted, not each context's latest"
        assert "github.sha" not in run, "on pull_request that is the merge commit"
        # A refused read fails the step: loud, never a silent skip.
        assert "|| true" not in run and "continue-on-error" not in step
        assert "shell" not in step, "the default bash -e is what fails a refused read"

    @pytest.mark.parametrize("name", ATTESTING_JOBS)
    def test_the_pytest_step_carries_the_reference_if(self, name: str) -> None:
        job = _jobs()[name]
        assert _squash(job["steps"][_pytest_at(job)]["if"]) == _squash(REFERENCE_IF)

    def test_the_no_skip_check_carries_the_same_if(self) -> None:
        """It reads the JUnit file the pytest step writes: skipped together, or red."""
        steps = _jobs()["integration"]["steps"]
        (check,) = [s for s in steps if "scripts/check_no_skips.py" in s.get("run", "")]
        assert _squash(check["if"]) == _squash(REFERENCE_IF)

    def test_only_the_heavy_steps_are_gated(self) -> None:
        jobs = _jobs()
        assert len(_gated_steps(jobs["test"])) == 1
        assert len(_gated_steps(jobs["integration"])) == 2
        for name in ALWAYS_RUN_JOBS:
            assert not _gated_steps(jobs[name]), name
            assert "attest" not in [s.get("id") for s in jobs[name]["steps"]], name

    @pytest.mark.parametrize("name", ATTESTING_JOBS)
    def test_the_copy_matches_the_vendored_reference(self, name: str) -> None:
        """Verbatim means verbatim: a submodule bump that changes upstream's CI side fails here."""
        if not ATTESTATION_MD.is_file():
            if INSTALLED_UNIT.exists():
                pytest.fail(f"{ATTESTATION_MD} missing on the host: git submodule update --init")
            pytest.skip("skills submodule not initialised")
        text = ATTESTATION_MD.read_text(encoding="utf-8").split("## The CI side", 1)[1]
        block = re.search(r"```yaml\n(.*?)```", text, flags=re.DOTALL)
        assert block, "attestation.md's CI side has no yaml block"
        ref_steps = yaml.safe_load(block[1])["jobs"]["test"]["steps"]
        (ref_attest,) = [s for s in ref_steps if s.get("id") == "attest"]
        (ref_gated,) = [s for s in ref_steps if "if" in s]

        def code(run: str) -> list[str]:
            return [ln.strip() for ln in run.splitlines() if not ln.strip().startswith("#")]

        job = _jobs()[name]
        at = _pytest_at(job)
        attest = job["steps"][at - 1]
        assert attest["env"] == ref_attest["env"]
        assert code(attest["run"]) == code(ref_attest["run"])
        assert _squash(ref_gated["if"]) == _squash(REFERENCE_IF)

    @pytest.mark.parametrize("name", ATTESTING_JOBS)
    def test_each_attesting_job_reads_statuses(self, name: str) -> None:
        assert _jobs()[name]["permissions"].get("statuses") == "read"

    def test_the_workflow_level_grant_is_unchanged(self) -> None:
        """A job's permissions replace the workflow's: statuses here would be inert."""
        workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
        assert workflow["permissions"] == {"contents": "read"}


class TestTheKnob:
    def test_the_knob_says_on(self) -> None:
        words = [
            word
            for line in KNOB.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
            for word in line.split()
        ]
        assert words == ["on"]

    def test_the_vendored_reader_says_on(self) -> None:
        """``_attest-gate.sh``'s own reader, which reads the knob from HEAD."""
        if not ATTEST_GATE_SH.is_file():
            if INSTALLED_UNIT.exists():
                pytest.fail(f"{ATTEST_GATE_SH} missing on the host: git submodule update --init")
            pytest.skip("skills submodule not initialised")
        out = subprocess.run(
            ["bash", "-c", '. "$1" && attest_read_knob', "_", str(ATTEST_GATE_SH)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert out.returncode == 0, out.stderr
