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

"Names" means the ``event_type`` of a ``WatchEvent(...)`` built in that
function, keyword or first positional — not any mention: a member compared
against (``prev == WatchEventType.X``) is not emitted, and counting it would
let a never-firing event pass.
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


def _event_type_arg(call: ast.Call) -> ast.expr | None:
    """The ``event_type`` argument of a ``WatchEvent(...)`` call, else None."""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name != "WatchEvent":
        return None
    for kw in call.keywords:
        if kw.arg == "event_type":
            return kw.value
    return call.args[0] if call.args else None


def _emitted_members(node: ast.AST) -> set[str]:
    """Members passed as a ``WatchEvent``'s ``event_type`` anywhere under ``node``."""
    emitted = set()
    for sub in ast.walk(node):
        arg = _event_type_arg(sub) if isinstance(sub, ast.Call) else None
        if (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.value.id == "WatchEventType"
        ):
            emitted.add(arg.attr)
    return emitted


def _sites_in(tree: ast.AST, module: str) -> dict[str, set[str]]:
    """``module:function`` → emitted member names, for each dispatching function.

    Same-named functions in one module (methods of two classes) share a key, so
    their sets merge rather than the later overwriting the earlier.
    """
    sites: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _calls_dispatch(node):
            sites.setdefault(f"{module}:{node.name}", set()).update(_emitted_members(node))
    return sites


def _dispatch_sites() -> dict[str, set[str]]:
    """``module:function`` → the WatchEventType member names it emits, over ``src/``."""
    sites: dict[str, set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for site, names in _sites_in(tree, str(path.relative_to(_SRC.parent))).items():
            sites.setdefault(site, set()).update(names)
    return sites


def _dispatched_values() -> set[str]:
    """Values of the emitted members; non-members are ``test_every_emitted_name_is_a_member``'s."""
    return {
        WatchEventType[name].value
        for names in _dispatch_sites().values()
        for name in names
        if name in WatchEventType.__members__
    }


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


# The scanner itself, on synthetic source: a guard that over-counts would pass
# an event that is only compared against — the #166 defect again.

_SITE = """
async def emit(session, prev):
    if prev == WatchEventType.WATCH_RECOVERED:
        pass
    await dispatch_event_notifications(
        session=session, event=WatchEvent(event_type=WatchEventType.CHANGE_DETECTED)
    )
"""


def test_a_compared_member_is_not_counted_as_emitted():
    assert _sites_in(ast.parse(_SITE), "m.py") == {"m.py:emit": {"CHANGE_DETECTED"}}


def test_a_positional_event_type_is_counted():
    source = """
async def emit(session):
    event = WatchEvent(WatchEventType.WATCH_ERROR, "id", "n", "u", now)
    await dispatch_event_notifications(session=session, event=event)
"""
    assert _sites_in(ast.parse(source), "m.py") == {"m.py:emit": {"WATCH_ERROR"}}


def test_same_named_sites_in_one_module_merge():
    source = f"""
class A:
{_SITE.replace(chr(10), chr(10) + "    ")}
class B:
{_SITE.replace("CHANGE_DETECTED", "WATCH_ERROR").replace(chr(10), chr(10) + "    ")}
"""
    assert _sites_in(ast.parse(source), "m.py") == {"m.py:emit": {"CHANGE_DETECTED", "WATCH_ERROR"}}


def test_every_emitted_name_is_a_member():
    unknown = {n for names in _dispatch_sites().values() for n in names} - set(
        WatchEventType.__members__
    )
    assert not unknown, f"WatchEvent(event_type=...) names non-members: {sorted(unknown)}"
