"""Pin the files Tailwind scans for class names (#351).

Tailwind v4 auto-detects sources across the whole repo unless told otherwise,
so a string in ``skills-vendor/``, ``tests/`` or ``docs/`` (``[tool:pytest]``,
from a submodule bump) became a CSS rule and left ``output.css`` stale.
``input.css`` therefore turns auto-detection off with ``source(none)`` and
lists every class-bearing source explicitly. These tests fail when a file under
``src/`` starts emitting class names without an ``@source`` that covers it.
See docs/STYLE.md §10. Pure file scans, no Tailwind CLI needed.
"""

import glob
import os
import re
from pathlib import Path

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
_CLASS_BEARING = re.compile(r"""\bclass\s*=\s*["']|\.className\b|\.classList\.""")
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
