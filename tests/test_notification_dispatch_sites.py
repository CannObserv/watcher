"""Guard: Watcher dispatches no notification of its own (#376, F3 part 2).

F3 handed notifications to Archiver at T = 2026-11-06T21:00:00Z. Archiver
notifies from the ``info.watch-events`` facts Watcher stages (#369), so
Watcher's three dispatch sites — ``CHANGE_DETECTED`` in the pipeline,
``WATCH_ERROR`` (with #71's reminder) and ``WATCH_RECOVERED`` in the fetch
apply — are gone, and with them the dispatcher, the T gate and the reminder
knob. A site that came back would send beside Archiver: every notification
twice.

It reads ``src/`` statically. Until #379 deletes the subsystem, the operator's
test-send routes still call ``dispatch_via_notifier`` directly; that is not
an automatic send and is not this guard's.

This file replaced #166's guard, which tied every subscribable event to a
dispatch site. Archiver owns that tie now.
"""

import ast
from pathlib import Path

from src.core.notifications.events import EVENT_TITLES
from src.dashboard.forms import ALL_EVENT_TYPE_VALUES

_SRC = Path(__file__).resolve().parent.parent / "src"
_DISPATCH = "dispatch_event_notifications"
_RETIRED_KNOBS = ("WATCHER_NOTIFY_CUTOVER_AT", "WATCHER_ERROR_RENOTIFY_INTERVAL")


def _sources() -> list[tuple[str, str]]:
    return [(str(p.relative_to(_SRC.parent)), p.read_text()) for p in sorted(_SRC.rglob("*.py"))]


def _names(tree: ast.AST) -> set[str]:
    """Every identifier a module calls, defines, imports or reads by attribute."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return names


def test_the_scan_sees_the_source_tree():
    """Non-vacuity: the scan reads the real tree, not an empty one."""
    paths = {path for path, _ in _sources()}
    assert {"src/workers/pipeline.py", "src/workers/fetch_commands.py"} <= paths


def test_no_module_names_the_dispatcher():
    offenders = [p for p, text in _sources() if _DISPATCH in _names(ast.parse(text, filename=p))]
    assert not offenders, f"{offenders} name {_DISPATCH}: Watcher's dispatch is Archiver's (#376)"


def test_no_module_reads_a_retired_knob():
    offenders = [(p, k) for p, text in _sources() for k in _RETIRED_KNOBS if k in text]
    assert not offenders, f"{offenders}: retired with Watcher's dispatch (#376 part 2)"


def test_the_scanner_finds_a_call_an_import_and_a_definition():
    """The scanner itself, on synthetic source: each form a site could take."""
    for source in (
        "async def f(s, e):\n    await dispatch_event_notifications(session=s, event=e)\n",
        "from src.core.notifications.notify import dispatch_event_notifications\n",
        "async def dispatch_event_notifications(session, event):\n    pass\n",
        "async def f(s, e):\n    await notify.dispatch_event_notifications(s, e)\n",
    ):
        assert _DISPATCH in _names(ast.parse(source)), source


def test_subscribe_checkboxes_offer_exactly_the_subscribable_events():
    """``EVENT_TITLES`` drives the template form's checkboxes (#166); #379 deletes both."""
    assert set(EVENT_TITLES) == set(ALL_EVENT_TYPE_VALUES)
