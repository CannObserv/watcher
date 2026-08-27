"""Every item-scoped audit event an operator can be shown must be filterable.

The global Audit Log derives its chip vocabulary from the data
(``get_distinct_audit_event_types``, #217), so it self-heals. The per-item
Recent Activity uses the hardcoded ``WATCHED_ITEM_EVENT_CHOICES``, which does
not: a new event type renders in the rows but is silently absent from the
filter. #274 walked into exactly that trap, and fixing the instance does not fix
the trap — this does.

It also surfaced a standing one: ``watched_item.throttled`` had been emitted with
a ``watched_item_id`` since #205 and never had a chip.
"""

import ast
import pathlib

from src.core.models.audit_log import EventType
from src.dashboard.context import WATCHED_ITEM_EVENT_CHOICES

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# Prefixes a single item's own activity is drawn from. `notification.*`,
# `domain.*` and `profile.*` are other surfaces' vocabularies.
ITEM_SCOPED_PREFIXES = ("check.", "watched_item.")

# The one item-scoped event with nowhere to be filtered: the detail page that
# would carry the chip 404s, because the row it belongs to is gone.
EXCLUDED = {EventType.WATCHED_ITEM_DELETED}


def _referenced_event_constants() -> set[str]:
    """Every ``EventType.X`` named anywhere under src/.

    Deliberately coarser than "constants passed to ``audit()``". Emission runs
    through helpers (``audit_event=`` kwargs, ternaries picking between two
    constants), so a call-shape scan missed four ``check.*`` events when this was
    written. Over-collecting only makes the assertion below stricter — a
    referenced-but-never-emitted constant costs one line in ``EXCLUDED``, while a
    missed one costs the silent gap this test exists to catch.
    """
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "EventType"
            ):
                found.add(node.attr)
    return found


def test_every_item_scoped_event_has_a_chip():
    emitted = {
        value
        for name in _referenced_event_constants()
        if (value := getattr(EventType, name)).startswith(ITEM_SCOPED_PREFIXES)
    }
    offered = {value for value, _ in WATCHED_ITEM_EVENT_CHOICES}

    missing = emitted - offered - EXCLUDED
    assert not missing, (
        f"emitted with no chip on the item detail page: {sorted(missing)}. "
        "Add to WATCHED_ITEM_EVENT_CHOICES, or to EXCLUDED with a reason."
    )


def test_the_scan_finds_something():
    """A guard whose scan silently returns nothing passes forever."""
    assert len(_referenced_event_constants()) > 10
