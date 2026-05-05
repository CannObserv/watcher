"""Heuristic selector proposal for InfoSpec authoring.

v1 ranks DOM elements whose visible text contains the supplied description
(case-insensitive substring) by:

- **Selector specificity** — id > stable class > tag-only.
- **Text length** — prefer elements whose text closely matches the description
  rather than huge ancestor blocks that happen to contain the phrase.
- **Volatility penalty** — elements with class names that look generated
  (hex/digit-only suffixes, ``hash-…`` prefixes) are demoted; those classes
  tend to break across deploys.

Out of scope here: render-then-replay stability checks, learned ranking
(see issue #146).
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from src.core.fetchers.base import FetchResult
from src.information.core.tools.fetch_and_render import HttpFetcherProtocol

_VOLATILE_TOKEN = re.compile(r"^(hash[-_])?[0-9a-f]{8,}$|^[0-9]{6,}$", re.IGNORECASE)
_SAMPLE_MAX_LEN = 200


@dataclass(frozen=True)
class SelectorCandidate:
    """One ranked selector candidate with sample text + stability score."""

    selector: str
    sample_text: str
    stability_score: float


def _is_volatile(class_name: str) -> bool:
    """True when ``class_name`` looks generated/hashed.

    Patterns covered: lone hex strings ≥ 8 chars, ``hash-…`` prefixes,
    and digit-only tokens ≥ 6 chars.
    """
    return bool(_VOLATILE_TOKEN.match(class_name))


def _build_selector(tag: Tag) -> str:
    """Build a CSS selector for ``tag``: ``#id`` > ``tag.class`` > ``tag``.

    Stops at the first stable identifier — id wins outright; otherwise the
    first non-volatile class is used; otherwise the bare tag name. Keeps
    selectors short and human-readable for operator review.
    """
    if tag.get("id"):
        return f"#{tag['id']}"
    classes = [c for c in (tag.get("class") or []) if not _is_volatile(c)]
    if classes:
        return f"{tag.name}.{classes[0]}"
    return tag.name


def _stability_score(tag: Tag, sample_text: str, description: str) -> float:
    """Compute a stability score in [0, 1] for ``tag``.

    Components:
    - id match: +0.4 (tags with ids are usually deliberate landmarks)
    - any non-volatile class: +0.3
    - text length proximity to description: +0.2 (shorter == tighter match)
    - volatility penalty: −0.3 if any class looks generated
    """
    score = 0.1  # base score for any matching tag
    classes = tag.get("class") or []
    if tag.get("id"):
        score += 0.4
    if any(not _is_volatile(c) for c in classes):
        score += 0.3
    # Volatility penalty: even if there's a stable class, the tag's overall
    # selector is shakier when *any* class is volatile.
    if any(_is_volatile(c) for c in classes):
        score -= 0.3

    # Text-length proximity: ratio of description length to sample length,
    # clamped — a tag whose only text is exactly the description scores
    # higher than an ancestor that contains the description plus 10× as much.
    if sample_text:
        ratio = min(len(description) / max(len(sample_text), 1), 1.0)
        score += 0.2 * ratio

    return max(0.0, min(1.0, score))


async def propose_selectors(
    fetcher: HttpFetcherProtocol,
    url: str,
    description: str,
    *,
    top_k: int = 5,
) -> list[SelectorCandidate]:
    """Fetch ``url`` and return up to ``top_k`` ranked selector candidates.

    Empty match set returns ``[]`` (not an error). Operators always verify the
    chosen selector via ``preview_extraction`` before persisting an InfoSpec.
    """
    if not description:
        return []
    fetch_result: FetchResult = await fetcher.fetch(url)
    soup = BeautifulSoup(fetch_result.content, "lxml")
    needle = description.lower()

    candidates: list[SelectorCandidate] = []
    for tag in soup.find_all(True):
        text = tag.get_text(" ", strip=True)
        if not text or needle not in text.lower():
            continue
        # Skip ancestors whose selected text is dominated by descendants we
        # already considered — we want the tightest containing element.
        if any(
            child.get_text(" ", strip=True) and needle in child.get_text(" ", strip=True).lower()
            for child in tag.find_all(True, recursive=False)
        ):
            continue
        sample = text[:_SAMPLE_MAX_LEN]
        candidates.append(
            SelectorCandidate(
                selector=_build_selector(tag),
                sample_text=sample,
                stability_score=_stability_score(tag, text, description),
            )
        )

    candidates.sort(key=lambda c: c.stability_score, reverse=True)
    return candidates[:top_k]
