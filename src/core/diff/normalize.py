"""Normalization stage: canonicalise input so diffs carry signal, not noise.

- normalize_text: canonicalise line endings + strip trailing whitespace per line.
- normalize_html: pretty-print HTML via html5lib (lenient parse) + lxml.html
  serialisation. Block-level elements get one per line; inline content stays
  put. Strips HTML comments (frequent noise). See #118 for motivation; #115
  Phase B reserves the structural-tree-diff path on top of this same parse.
"""

import html5lib
from lxml import html as lxml_html

# Elements where whitespace inside (text) is significant per HTML spec —
# we never collapse text or tail inside these. Comments are always stripped.
_WS_PRESERVE_TAGS = frozenset({"pre", "textarea", "script", "style"})


def normalize_text(text: str) -> str:
    """Canonicalise newlines (CRLF/CR → LF) and strip per-line trailing whitespace."""
    if not text:
        return text
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in unified.split("\n"))


def _has_preserve_ancestor(elem) -> bool:
    a = elem.getparent()
    while a is not None:
        if a.tag in _WS_PRESERVE_TAGS:
            return True
        a = a.getparent()
    return False


def _strip_structural_whitespace(tree) -> None:
    """Drop whitespace-only ``text`` and ``tail`` outside whitespace-preserving
    contexts. ``text`` belongs to the element itself; ``tail`` belongs to the
    PARENT context, so each side checks the appropriate scope.
    """
    for elem in tree.iter():
        parent = elem.getparent()
        parent_preserves = parent is not None and (
            parent.tag in _WS_PRESERVE_TAGS or _has_preserve_ancestor(parent)
        )
        if elem.tail is not None and not elem.tail.strip() and not parent_preserves:
            elem.tail = None
        elem_preserves = elem.tag in _WS_PRESERVE_TAGS or _has_preserve_ancestor(elem)
        if elem.text is not None and not elem.text.strip() and not elem_preserves:
            elem.text = None


def normalize_html(html: str) -> str:
    """Pretty-print HTML so diffs are readable instead of one-very-long-line.

    Parses with html5lib (tolerates malformed input — unclosed tags, weird
    nesting, real-world web pages), serialises via ``lxml.html.tostring`` with
    ``pretty_print=True`` so block-level elements occupy their own line while
    inline content (``<b>``, ``<a>``, ``<em>``…) stays put. Strips HTML
    comments, since regenerated comments (timestamps, build IDs) are a common
    source of noise that drowns out real changes.

    Empty / whitespace-only input is passed through unchanged so we don't
    inflate "no content" snapshots into bare ``<html><body></body></html>``
    skeletons that would falsely appear identical to one another.

    The pipeline runs twice: lxml's ``pretty_print`` introduces structural
    whitespace that on a re-parse can shift, so a second pass stabilises
    output to a fixed point. Result is idempotent — calling on already
    pretty-printed input produces the same output. Whitespace inside
    ``<pre>``, ``<textarea>``, ``<script>``, ``<style>`` is preserved.
    """
    if not html or not html.strip():
        return html

    def _pass(text: str):
        tree = html5lib.parse(text, treebuilder="lxml", namespaceHTMLElements=False)
        for comment in tree.xpath("//comment()"):
            comment.getparent().remove(comment)
        _strip_structural_whitespace(tree)
        return lxml_html.tostring(tree, pretty_print=True, encoding="unicode")

    return _pass(_pass(html))
