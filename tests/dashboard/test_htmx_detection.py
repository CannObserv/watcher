"""Guard the canonical HTMX-detection idiom (#211).

Dashboard routes must detect HTMX via ``src.dashboard.deps.is_htmx`` — which
applies the ``HX-Boosted`` guard required by the AGENTS.md HTMX convention — not
a bare ``request.headers.get("HX-Request")`` check. A bare check treats a
boosted full-page navigation as an inline fragment swap. ``deps.py`` is the one
module allowed to read the raw headers (it *is* the helper). This is a pure file
scan (no DB) so it runs in the default suite on every ``uv run pytest``.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = _ROOT / "src" / "dashboard"

# The canonical helper lives here and is the sole place allowed to read the raw
# HX-Request / HX-Boosted headers. Scope is dashboard-only by design — API
# routes never render partials, so they have no HTMX-detection concern.
_HELPER_MODULE = DASHBOARD_DIR / "deps.py"

# Bare reads of the HTMX request headers — what `is_htmx` exists to replace.
# Receiver-agnostic (any `<obj>.headers.get`) and quote-agnostic so a rename
# (`req`) or single quotes can't slip a bare check past the guard.
_BARE_HEADER = re.compile(r"""\.headers\.get\(\s*["']HX-(?:Request|Boosted)""")


def _offending_lines():
    """Yield (relpath, line_no, text) for bare HX-* header reads outside the helper."""
    for path in sorted(DASHBOARD_DIR.rglob("*.py")):
        if path == _HELPER_MODULE:
            continue
        rel = path.relative_to(_ROOT)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _BARE_HEADER.search(line):
                yield rel, n, line.strip()


def test_no_bare_htmx_header_checks():
    offenders = list(_offending_lines())
    detail = "\n".join(f"  {rel}:{n}: {text}" for rel, n, text in offenders)
    assert not offenders, (
        "Use src.dashboard.deps.is_htmx instead of a bare HX-* header read:\n" + detail
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'request.headers.get("HX-Request")',
        "request.headers.get('HX-Request')",  # single quotes
        'req.headers.get("HX-Boosted")',  # renamed receiver
        'request.headers.get(  "HX-Request"  )',  # whitespace padding
    ],
)
def test_regex_catches_bare_read_variants(snippet):
    assert _BARE_HEADER.search(snippet), f"guard should flag: {snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        'request.headers.get("Content-Type")',
        "is_htmx(request)",
        'request.headers.get("X-ExeDev-UserID")',
    ],
)
def test_regex_ignores_unrelated_header_reads(snippet):
    assert not _BARE_HEADER.search(snippet), f"guard should ignore: {snippet}"
