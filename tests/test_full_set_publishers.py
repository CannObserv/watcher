"""The full-set publish helpers may only be called with the full set (#292, CR 1).

``publish_policy_events`` and ``publish_status_events`` derive their retention
floor from the batch they are handed — ``len(events) * RETAINED_FULL_SETS``. That
is the corpus only while the batch *is* the corpus. A partial batch floors the
cap at a fraction of the set and the publish then trims the set down to it: the
silent partial-replay failure the floor exists to prevent, arriving through the
parameter that implements it.

Both helpers' docstrings state the precondition, and prose is not a guard. What
makes it one is that each has exactly one caller — its own full-set function,
which reads every row. This reads the source rather than the runtime because the
failure it guards against is a *new* call site written the wrong way, which no
existing test would exercise; it is the same reasoning as
``tests/test_bus_stream_kinds.py``, and it asserts it found the call sites so a
rename fails loudly instead of passing vacuously.

The cheaper-looking alternative — have the helpers take the set size, or assert
on it — does not exist: nothing inside the helper can tell a complete sequence
from an incomplete one. The knowledge lives at the only place that queried the
rows, so the rule has to be about *who calls*.
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Same boundary as tests/test_bus_stream_kinds.py: anything that can reach the
# broker, which since #262 explicitly includes a one-off script.
SCANNED_ROOTS = (ROOT / "src", ROOT / "scripts")

#: helper → the single function allowed to call it. A helper whose floor is
#: derived from its argument belongs here; one that takes an explicit cap does
#: not, which is why this is a mapping rather than a blanket rule about names.
FULL_SET_PUBLISHERS = {
    "publish_policy_events": "publish_full_policy_set",
    "publish_status_events": "publish_full_status_set",
}


def _callee(call: ast.Call) -> str | None:
    """The called name, bare or qualified — ``f()`` and ``mod.f()`` both resolve.

    Matching on the final attribute is the same residual looseness
    ``test_bus_stream_kinds`` accepts: an unrelated method of the same name would
    pass, which is not a state anyone reaches by accident.
    """
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _call_sites():
    """Every call to a full-set publisher, with the function that contains it.

    Yields ``(path, callee, enclosing)`` where ``enclosing`` is None for a call
    at module scope — which is never allowed, and would otherwise be skipped by a
    rule that only looked inside function bodies.
    """
    for root in SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text())
            enclosing: dict[int, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    for child in ast.walk(node):
                        # Nearest enclosing definition wins, so a nested def is
                        # reported as itself rather than as its parent.
                        if isinstance(child, ast.Call):
                            enclosing.setdefault(id(child), node.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    callee = _callee(node)
                    if callee in FULL_SET_PUBLISHERS:
                        yield path, callee, enclosing.get(id(node))


def test_full_set_publishers_are_called_only_by_their_full_set_function():
    found: set[str] = set()
    for path, callee, enclosing in _call_sites():
        allowed = FULL_SET_PUBLISHERS[callee]
        assert enclosing == allowed, (
            f"{path.relative_to(ROOT)}: {callee}() called from "
            f"{enclosing or '<module scope>'!r}, but only {allowed}() may call it. Its "
            "retention floor is derived from the batch it is handed "
            "(len(events) * RETAINED_FULL_SETS), so a partial batch floors the cap at a "
            "fraction of the set and the publish trims the set down to it — a boot replay "
            "then returns a partial set no consumer can tell from a complete one (#292). "
            "Publish the full set, or give the new path its own cap."
        )
        found.add(callee)
    assert found == set(FULL_SET_PUBLISHERS), (
        f"expected a call site for each of {sorted(FULL_SET_PUBLISHERS)}, found "
        f"{sorted(found)} — has one been renamed? A vacuous pass here is the whole "
        "failure mode this assertion exists to prevent."
    )


@pytest.mark.parametrize("helper", sorted(FULL_SET_PUBLISHERS))
def test_the_precondition_is_stated_where_the_caller_reads_it(helper):
    """The guard is not the documentation — the docstring has to say it too.

    Someone handed a failure from the rule above reads the helper's docstring to
    find out why, and a rule whose reason lives only in a test file is how the
    next author concludes the restriction is arbitrary and deletes it.
    """
    for root in SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == helper:
                    doc = ast.get_docstring(node) or ""
                    assert "must be the complete" in doc, (
                        f"{path.relative_to(ROOT)}: {helper}'s docstring does not state "
                        "that its argument must be the complete set, which is the "
                        "precondition its retention floor rests on (#292, CR 1)."
                    )
                    return
    pytest.fail(f"{helper} not found in {[str(r) for r in SCANNED_ROOTS]} — renamed?")
