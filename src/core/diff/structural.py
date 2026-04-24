"""Structural (tree) diff for HTML.

Phase A: stub. Phase B replaces with xmldiff-based tree edit-distance diff.
See issue #115 for the rollout plan.
"""


def compute_html_tree_diff(previous: str, current: str) -> list:
    """Compute a structural diff between two HTML documents.

    Phase A stub — always raises. Phase B implements via `xmldiff`.
    """
    raise NotImplementedError("compute_html_tree_diff is reserved for issue #115 Phase B (xmldiff)")
