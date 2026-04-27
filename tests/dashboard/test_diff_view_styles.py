"""Cascade-layer regression tests for the diff2html viewer styling.

Issue #120: Tailwind v4 preflight (`@layer base`) zeros padding/border on
`*`. The vendored diff2html stylesheet sits in the lower-priority
`@layer vendor`, so the preflight wins and strips the padding diff2html
relies on to make space for its absolutely-positioned line numbers — the
line-number column then paints over the start of every content row.

These tests assert that the *components* layer (which sorts above
*base*) re-asserts the padding/border that diff2html needs. We check the
compiled `output.css` rather than `input.css` so the test catches both
authoring mistakes and a stale build artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_CSS = REPO_ROOT / "src" / "dashboard" / "static" / "css" / "output.css"


@pytest.fixture(scope="module")
def components_layer() -> str:
    """Return the body of the first `@layer components { … }` block in
    `output.css`. Brace-balanced extraction so nested at-rules don't
    truncate the slice."""
    css = OUTPUT_CSS.read_text(encoding="utf-8")
    start = css.find("@layer components{")
    assert start != -1, "@layer components block not found in output.css"
    body_start = css.index("{", start) + 1
    i = body_start
    depth = 1
    while i < len(css) and depth:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "Unbalanced braces in @layer components block"
    return css[body_start : i - 1]


def _padding_inline_start(rule_body: str) -> str | None:
    """Return the rightmost-defined inline-start padding value in a CSS
    rule body, or None if no padding is set. Supports `padding`,
    `padding-inline`, `padding-inline-start`, and `padding-left`. Returns
    the literal string (e.g. ``"4.5em"``) — caller decides what counts as
    non-zero."""
    last: str | None = None
    for m in re.finditer(r"(padding(?:-inline(?:-start)?|-left)?)\s*:\s*([^;}]+)", rule_body):
        prop, val = m.group(1), m.group(2).strip()
        if prop == "padding":
            tokens = val.split()
            last = tokens[3] if len(tokens) == 4 else tokens[1] if len(tokens) >= 2 else tokens[0]
        elif prop == "padding-inline":
            tokens = val.split()
            last = tokens[1] if len(tokens) >= 2 else tokens[0]
        else:
            last = val
    return last


def _is_zero(value: str | None) -> bool:
    if value is None:
        return True
    return bool(re.fullmatch(r"0(?:px|em|rem|%)?", value.strip()))


def _rules_for_selector(layer_body: str, selector_re: str) -> list[str]:
    """Return the bodies of every rule whose selector list matches
    `selector_re`, in source order. We walk the layer body brace-by-brace
    so nested rules don't confuse the match."""
    out: list[str] = []
    i = 0
    while i < len(layer_body):
        brace = layer_body.find("{", i)
        if brace == -1:
            break
        selector = layer_body[i:brace]
        depth = 1
        j = brace + 1
        while j < len(layer_body) and depth:
            if layer_body[j] == "{":
                depth += 1
            elif layer_body[j] == "}":
                depth -= 1
            j += 1
        body = layer_body[brace + 1 : j - 1]
        if re.search(selector_re, selector):
            out.append(body)
        i = j
    return out


def _last_rule_for_selector(layer_body: str, selector_re: str) -> str | None:
    rules = _rules_for_selector(layer_body, selector_re)
    return rules[-1] if rules else None


@pytest.mark.parametrize(
    ("selector_re", "label"),
    [
        (r"\.d2h-code-side-line\b", "side-by-side content cell"),
        (r"\.d2h-code-line\b", "line-by-line content cell"),
    ],
)
def test_diff2html_content_cells_have_inline_start_padding(
    components_layer: str, selector_re: str, label: str
) -> None:
    body = _last_rule_for_selector(components_layer, selector_re)
    assert body is not None, (
        f"@layer components has no rule for {label} ({selector_re}); "
        "Tailwind preflight zeros its padding and the absolutely-positioned "
        "line number overlays the content."
    )
    value = _padding_inline_start(body)
    assert not _is_zero(value), (
        f"{label} rule in @layer components has zero inline-start padding "
        f"(got {value!r}); preflight is winning the cascade."
    )


def test_diff2html_side_linenumber_keeps_vertical_border(
    components_layer: str,
) -> None:
    """The line-number cell needs vertical column rules (diff2html ships
    `border-width: 0 1px`). Preflight's `border: 0 solid` strips the width
    in @layer base, so a components-layer rule must reassert a non-zero
    inline border width — `border-inline: 1px ...` (shorthand) or an
    explicit `border-(left|right|inline-...)-width`."""
    rules = _rules_for_selector(components_layer, r"\.d2h-code-side-linenumber\b")
    assert rules, (
        "@layer components has no rule for .d2h-code-side-linenumber; "
        "preflight `border: 0 solid` removes the column rules diff2html "
        "draws around the line-number cell."
    )
    width_re = re.compile(
        r"border(?:-(?:left|right|inline(?:-start|-end)?))?\s*:\s*[^;}]*\b\d"
        r"|border(?:-(?:left|right|inline(?:-start|-end)?))?-width\s*:\s*[^;}]*\b\d"
    )
    assert any(width_re.search(body) for body in rules), (
        "No components-layer rule for .d2h-code-side-linenumber sets a "
        "non-zero inline border width; preflight is winning the cascade."
    )
