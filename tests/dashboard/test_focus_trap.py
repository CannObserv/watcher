"""Static guards for modal focus management (#39).

Any element declaring ``aria-modal="true"`` promises assistive tech that the
rest of the page is inert. The shared ``focus-trap.js`` utility is what makes
that true, so every such element must reference its hook (``data-focus-trap``)
— this is what stops modal #3 shipping with dialog semantics and no behavior.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parents[2] / "src" / "dashboard" / "templates"

TAG_WITH_ARIA_MODAL = re.compile(r"<[^>]*aria-modal=\"true\"[^>]*>", re.DOTALL)


def _aria_modal_tags():
    found = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        for match in TAG_WITH_ARIA_MODAL.finditer(template.read_text()):
            found.append((template.relative_to(TEMPLATES), match.group(0)))
    return found


class TestFocusTrapHook:
    def test_templates_declare_at_least_one_modal(self):
        """Sanity: the scan finds the known dialogs (drawer + API-key modal)."""
        assert len(_aria_modal_tags()) >= 2

    def test_every_aria_modal_element_references_focus_trap(self):
        """aria-modal="true" without data-focus-trap asserts inertness it can't deliver."""
        missing = [(path, tag) for path, tag in _aria_modal_tags() if "data-focus-trap" not in tag]
        assert not missing, (
            "Elements declare aria-modal but lack the data-focus-trap hook "
            f"(WCAG 2.4.3 / 2.1.2 — see #39): {missing}"
        )

    def test_base_template_loads_focus_trap_js(self):
        """focus-trap.js must load deferred with cache-busting (STYLE.md §10)."""
        base = (TEMPLATES / "base.html").read_text()
        assert re.search(
            r"<script src=\"/static/js/focus-trap\.js\?v={{ build_id }}\" defer></script>",
            base,
        ), "base.html must load focus-trap.js with defer and ?v={{ build_id }}"


class TestApiKeyModalEscapeSemantics:
    """Closing the one-time key modal without copying destroys the key (#39).

    The shared utility closes on Escape by default; this modal must opt out so
    key loss is never a one-keystroke action.
    """

    def test_api_key_modal_suppresses_escape(self):
        partial = (TEMPLATES / "partials" / "api_key_new_key_modal.html").read_text()
        tags = TAG_WITH_ARIA_MODAL.findall(partial)
        assert len(tags) == 1
        assert 'data-focus-trap-escape="ignore"' in tags[0]

    def test_api_key_modal_initial_focus_is_key_input(self):
        """Initial focus lands on the key input so the value is immediately selectable."""
        partial = (TEMPLATES / "partials" / "api_key_new_key_modal.html").read_text()
        input_tag = re.search(r"<input[^>]*id=\"new-api-key-value\"[^>]*>", partial, re.DOTALL)
        assert input_tag is not None
        assert "data-focus-trap-initial" in input_tag.group(0)
