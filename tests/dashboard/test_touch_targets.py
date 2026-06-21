"""Guard the single touch-target / button min-height idiom (#203).

Component classes (``.btn*``, ``.segment``, ``.chip``, ``.form-input``,
``.toggle``, nav-link) own the WCAG 2.1 AA 44px min-height. Explicit
``min-h-[44px]`` is reserved for bare interactive elements (``<a>``,
``<label>``, component-less ``<button>``); restating it on a ``.btn`` is
redundant, and ``min-h-0`` shrinks a component target below 44px. See
docs/STYLE.md. These are pure file scans (no DB) so they run in the default
suite on every ``uv run pytest``.
"""

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "templates"
_BTN_TOKEN = re.compile(r"\bbtn\b")


def _template_lines():
    """Yield (relpath, line_no, text) for every line in every dashboard template."""
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        rel = path.relative_to(TEMPLATES_DIR)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            yield rel, n, line


class TestTouchTargetIdiom:
    def test_no_redundant_min_height_on_btn(self):
        """A .btn already guarantees 44px — never restate min-h-[44px] on it.

        Per-line check: assumes a `.btn` element's class string (the `btn` token
        and any `min-h-[44px]`) sits on one physical line — the codebase
        convention. A `.btn` split across lines with `min-h-[44px]` on a
        continuation line would not be flagged.
        """
        offenders = [
            f"{rel}:{n}"
            for rel, n, line in _template_lines()
            if "min-h-[44px]" in line and _BTN_TOKEN.search(line)
        ]
        assert not offenders, (
            "Redundant min-h-[44px] on .btn elements (the .btn class already "
            f"guarantees 44px): {offenders}"
        )

    def test_no_min_h_0(self):
        """min-h-0 shrinks a component target below 44px — a latent a11y bug."""
        offenders = [f"{rel}:{n}" for rel, n, line in _template_lines() if "min-h-0" in line]
        assert not offenders, f"min-h-0 found (shrinks targets below 44px): {offenders}"
