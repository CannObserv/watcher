"""Normalization stage: canonicalise input so diffs carry signal, not noise.

- normalize_text: canonicalise line endings + strip trailing whitespace per line.
- normalize_html: Phase A passthrough. Phase B replaces with lxml/html5lib
  pretty-print + attribute sort + comment strip (see issue #115).
"""


def normalize_text(text: str) -> str:
    """Canonicalise newlines (CRLF/CR → LF) and strip per-line trailing whitespace."""
    if not text:
        return text
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in unified.split("\n"))


def normalize_html(html: str) -> str:
    """Phase A stub: passthrough. Phase B will add structural normalization."""
    return html
