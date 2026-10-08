"""Pin the files Tailwind scans for class names (#351).

Tailwind v4 auto-detects sources across the whole repo unless told otherwise,
so a string in ``skills-vendor/``, ``tests/`` or ``docs/`` (``[tool:pytest]``,
from a submodule bump) became a CSS rule and left ``output.css`` stale.
``input.css`` therefore turns auto-detection off with ``source(none)`` and
lists every class-bearing source explicitly. These tests fail when a file under
``src/`` starts emitting class names without an ``@source`` that covers it.
See docs/STYLE.md §10. The second half pins the gates that run
``scripts/check-css.sh`` (#352): the pre-commit hook's ``files`` filter and
the CI ``css`` job. Pure file scans, no Tailwind CLI needed.
"""

import glob
import os
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = _ROOT / "src"
CSS_DIR = SRC_DIR / "dashboard" / "static" / "css"
INPUT_CSS = CSS_DIR / "input.css"

# Third-party files: their class strings are not ours to compile, so no
# @source may cover them.
VENDORED = (
    SRC_DIR / "dashboard" / "static" / "js" / "htmx.min.js",
    SRC_DIR / "dashboard" / "static" / "js" / "vendor",
)

# A class attribute (templates, HTML built in Python) or a DOM class write (JS).
_CLASS_BEARING = re.compile(
    r"""\bclass\s*=\s*["']|\.className\b|\.classList\.|setAttribute\(\s*["']class["']"""
)
_SOURCE = re.compile(r'@source\s+(not\s+)?"([^"]+)"\s*;')


def _input_css() -> str:
    return INPUT_CSS.read_text(encoding="utf-8")


def _expand(pattern: str) -> set[Path]:
    """Resolve an ``@source`` glob, relative to input.css, to the files it matches."""
    full = os.path.normpath(CSS_DIR / pattern)
    if os.path.isdir(full):
        full = os.path.join(full, "**", "*")
    return {Path(p) for p in glob.glob(full, recursive=True) if os.path.isfile(p)}


def _scanned() -> set[Path]:
    """Return every file Tailwind's ``@source`` directives cover."""
    included: set[Path] = set()
    excluded: set[Path] = set()
    for negated, pattern in _SOURCE.findall(_input_css()):
        (excluded if negated else included).update(_expand(pattern))
    return included - excluded


def _is_vendored(path: Path) -> bool:
    return any(path == v or v in path.parents for v in VENDORED)


def _class_bearing_files() -> set[Path]:
    """Return every authored file under ``src/`` that names CSS classes."""
    found = set()
    for path in SRC_DIR.rglob("*"):
        if path.suffix not in {".html", ".py", ".js"} or _is_vendored(path):
            continue
        if _CLASS_BEARING.search(path.read_text(encoding="utf-8")):
            found.add(path)
    return found


def test_auto_detection_is_off():
    """``@import "tailwindcss" source(none)``: only the @source lines are scanned."""
    assert re.search(r'@import\s+"tailwindcss"\s+source\(none\)\s*;', _input_css())


def test_every_class_bearing_file_is_scanned():
    """A file under ``src/`` that names classes is covered by an ``@source``."""
    missing = sorted(str(p.relative_to(_ROOT)) for p in _class_bearing_files() - _scanned())
    assert not missing, f"class-bearing files no @source covers: {missing}"


@pytest.mark.parametrize(
    "snippet",
    [
        '<p class="text-red-600">',
        "el.className = 'flash';",
        'el.classList.add("hidden");',
        'el.setAttribute("class", "hidden");',
    ],
)
def test_class_bearing_pattern_sees_every_form(snippet: str):
    """Attribute, ``className``, ``classList`` and ``setAttribute`` all name classes."""
    assert _CLASS_BEARING.search(snippet)


def test_python_class_emitter_is_detected():
    """The scan sees HTML built in Python, not only templates and JS."""
    assert SRC_DIR / "dashboard" / "routes" / "domains.py" in _class_bearing_files()


def test_no_source_outside_the_dashboard():
    """Nothing outside ``src/dashboard`` is scanned: no tests, docs or skills-vendor."""
    outside = sorted(
        str(p.relative_to(_ROOT)) for p in _scanned() if SRC_DIR / "dashboard" not in p.parents
    )
    assert not outside, f"@source reaches outside src/dashboard: {outside}"


def test_vendored_files_are_not_scanned():
    """Third-party JS contributes no utilities."""
    vendored = sorted(str(p.relative_to(_ROOT)) for p in _scanned() if _is_vendored(p))
    assert not vendored, f"@source covers vendored files: {vendored}"


# --- The gate that runs check-css.sh (#352) ---------------------------------

PRE_COMMIT_CONFIG = _ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
BUILD_CSS = _ROOT / "scripts" / "build-css.sh"
_CLI_PIN = re.compile(r"@tailwindcss/cli@(\d+\.\d+\.\d+)")


def _css_hook_files() -> re.Pattern[str]:
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] for h in repo["hooks"]]
    (hook,) = [h for h in hooks if h["id"] == "tailwind-css"]
    assert hook["entry"] == "bash scripts/check-css.sh"
    return re.compile(hook["files"])


def test_css_hook_fires_on_every_build_input():
    """A commit touching any scanned source, the CSS or the build scripts runs the check."""
    files = _css_hook_files()
    inputs = _scanned() | {
        INPUT_CSS,
        CSS_DIR / "output.css",
        BUILD_CSS,
        _ROOT / "scripts" / "check-css.sh",
    }
    silent = sorted(
        rel for rel in (str(p.relative_to(_ROOT)) for p in inputs) if not files.search(rel)
    )
    assert not silent, f"tailwind-css hook skips: {silent}"


def _css_ci_job() -> dict:
    """Return the one CI job that runs ``scripts/check-css.sh``."""
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"].values()
    (job,) = [
        job
        for job in jobs
        if any("scripts/check-css.sh" in step.get("run", "") for step in job["steps"])
    ]
    return job


def test_ci_runs_check_css_with_the_pinned_cli():
    """CI gates output.css, installing the same CLI version build-css.sh pins."""
    (pin,) = set(_CLI_PIN.findall(BUILD_CSS.read_text(encoding="utf-8")))
    installs = {v for step in _css_ci_job()["steps"] for v in _CLI_PIN.findall(step.get("run", ""))}
    assert installs == {pin}


# Every file that tells a reader which CLI to install. An unpinned hint installs
# the latest CLI, whose build check-css.sh then calls stale.
_CLI_HINTS = (
    BUILD_CSS,
    _ROOT / "scripts" / "check-css.sh",
    CI_WORKFLOW,
    _ROOT / "AGENTS.md",
    _ROOT / "docs" / "COMMANDS.md",
)


def test_every_cli_install_hint_carries_the_one_pin():
    """``@tailwindcss/cli`` is installed at one version wherever it is named."""
    pins: dict[str, set[str]] = {}
    for path in _CLI_HINTS:
        text = path.read_text(encoding="utf-8")
        unpinned = re.findall(r"install -g @tailwindcss/cli(?!@)", text)
        assert not unpinned, f"{path.relative_to(_ROOT)} names the CLI without a pin"
        pins[str(path.relative_to(_ROOT))] = set(_CLI_PIN.findall(text))
    assert all(pins.values()), f"a hint file names no pinned CLI: {pins}"
    assert len(set().union(*pins.values())) == 1, f"CLI pins disagree: {pins}"


def test_ci_css_job_holds_no_cloud_credentials():
    """The CSS job needs Node and the CLI only: no OIDC token, no wheelhouse."""
    job = _css_ci_job()
    assert job.get("permissions") == {"contents": "read"}
    text = yaml.safe_dump(job)
    assert "google-github-actions" not in text
    assert "sync_wheelhouse" not in text
