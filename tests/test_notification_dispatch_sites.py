"""Guard: every subscribable notification event has a dispatch site (#166).

Five of eight ``WatchEventType`` members were tickable in the template form,
carried default bodies and preview fixtures, and never fired: nothing tied the
subscribable list to the code that emits. This test is that tie.

It reads ``src/`` statically. A *dispatch site* is a function that calls
``dispatch_event_notifications``; the events it emits are the ``WatchEventType``
members that function names. Every subscribable value must be emitted by some
site, and every site must name the member it emits — one that dispatches an
event handed to it from elsewhere cannot be attributed, so it fails here until
this guard learns to follow it.
"""

import ast
from pathlib import Path

from src.core.notifications.events import EVENT_TITLES, WatchEventType
from src.dashboard.forms import ALL_EVENT_TYPE_VALUES

_SRC = Path(__file__).resolve().parent.parent / "src"
_DISPATCH = "dispatch_event_notifications"


def _calls_dispatch(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == _DISPATCH:
                return True
    return False


def _members_named(node: ast.AST) -> set[str]:
    return {
        sub.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute)
        and isinstance(sub.value, ast.Name)
        and sub.value.id == "WatchEventType"
    }


def _dispatch_sites() -> dict[str, set[str]]:
    """``module:function`` → the WatchEventType member names it emits."""
    sites: dict[str, set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _calls_dispatch(node):
                sites[f"{path.relative_to(_SRC.parent)}:{node.name}"] = _members_named(node)
    return sites


def _dispatched_values() -> set[str]:
    return {WatchEventType[name].value for names in _dispatch_sites().values() for name in names}


def test_dispatch_sites_are_found():
    """Non-vacuity: the scan sees the real call sites, not an empty tree."""
    sites = _dispatch_sites()
    modules = {site.split(":", 1)[0] for site in sites}
    assert {"src/workers/fetch_commands.py", "src/workers/pipeline.py"} <= modules, sites


def test_every_dispatch_site_names_its_event():
    unattributable = [site for site, names in _dispatch_sites().items() if not names]
    assert not unattributable, (
        f"{unattributable} dispatch without naming a WatchEventType member; "
        "teach this guard where their event comes from"
    )


def test_every_subscribable_event_is_dispatched():
    never_fires = set(ALL_EVENT_TYPE_VALUES) - _dispatched_values()
    assert not never_fires, (
        f"{sorted(never_fires)} are subscribable but no code dispatches them (#166): "
        "add a dispatch site, or take them off WatchEventType"
    )


def test_every_dispatched_event_is_subscribable():
    assert _dispatched_values() <= set(ALL_EVENT_TYPE_VALUES)


def test_subscribe_checkboxes_offer_exactly_the_subscribable_events():
    """``EVENT_TITLES`` drives the checkboxes; it may not offer more or fewer."""
    assert set(EVENT_TITLES) == set(ALL_EVENT_TYPE_VALUES)
