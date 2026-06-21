"""Guard the touch-target / button min-height idiom (#203).

Component classes (``.btn*``, ``.nav-link``, ``.segment span``, ``.chip span``,
``.form-input``, ``.toggle``) own the WCAG 2.1 AA 44px min-height. Explicit
``min-h-[44px]`` is reserved for bare interactive elements (``<a>``,
``<label>``, component-less ``<button>``); restating it on a component class is
redundant, and ``min-h-0`` shrinks a component target below 44px. See
docs/STYLE.md §7. These are pure file scans (no DB) so they run in the default
suite on every ``uv run pytest``.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = _ROOT / "src" / "dashboard" / "templates"
INPUT_CSS = _ROOT / "src" / "dashboard" / "static" / "css" / "input.css"

# Component classes that bake in min-h-[44px] (see docs/STYLE.md §7). Restating
# min-h-[44px] on any of these in a template is redundant.
_COMPONENT_CLASS_TOKEN = re.compile(r"\b(?:btn|nav-link|segment|chip|form-input|toggle)\b")

# The exact input.css selectors whose blocks must carry the 44px guarantee.
COMPONENT_CLASS_SELECTORS = [
    ".btn",
    ".nav-link",
    ".segment span",
    ".chip span",
    ".form-input",
    ".toggle",
]


def _template_lines():
    """Yield (relpath, line_no, text) for every line in every dashboard template."""
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        rel = path.relative_to(TEMPLATES_DIR)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            yield rel, n, line


def _css_block(selector):
    """Return the body of the first ``selector { ... }`` block in input.css."""
    header = f"{selector} {{"
    body = []
    inside = False
    for line in INPUT_CSS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not inside:
            if stripped == header:
                inside = True
            continue
        if "}" in stripped:
            break
        body.append(line)
    return "\n".join(body)


class TestTouchTargetIdiom:
    def test_no_redundant_min_height_on_component_class(self):
        """A component class already guarantees 44px — never restate min-h-[44px].

        Per-line check: assumes a component element's class string (the class
        token and any ``min-h-[44px]``) sits on one physical line — the codebase
        convention. An element split across lines with ``min-h-[44px]`` on a
        continuation line would not be flagged.
        """
        offenders = [
            f"{rel}:{n}"
            for rel, n, line in _template_lines()
            if "min-h-[44px]" in line and _COMPONENT_CLASS_TOKEN.search(line)
        ]
        assert not offenders, (
            "Redundant min-h-[44px] on a component class that already guarantees "
            f"44px (see docs/STYLE.md §7): {offenders}"
        )

    def test_no_min_h_0(self):
        """min-h-0 shrinks a component target below 44px — a latent a11y bug."""
        offenders = [f"{rel}:{n}" for rel, n, line in _template_lines() if "min-h-0" in line]
        assert not offenders, f"min-h-0 found (shrinks targets below 44px): {offenders}"


class TestComponentClassesProvide44px:
    """The CSS side of the rule: each documented component class must actually
    bake in min-h-[44px]. Closes the doc<->CSS loop — would have caught the
    pre-fix .toggle gap (docs claimed 44px, CSS gave a 24px track).
    """

    @pytest.mark.parametrize("selector", COMPONENT_CLASS_SELECTORS)
    def test_component_class_bakes_min_height(self, selector):
        block = _css_block(selector)
        assert block, f"`{selector} {{` block not found in input.css"
        assert "min-h-[44px]" in block, (
            f"{selector} no longer bakes in min-h-[44px]; docs/STYLE.md §7 claims "
            "it owns the 44px guarantee. Restore the token or update the docs."
        )
