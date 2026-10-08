"""Fail a pytest run that skipped anything, read from its JUnit XML report (#353).

A test that skips itself reports green while verifying nothing, so a gate that
only fails on failures can claim "runs in CI" for a test that never ran. CI's
``integration`` job and ``scripts/pre-ship.sh`` run this after
``pytest -m integration --junitxml=<report>``. A test that genuinely needs an
external service belongs under the ``live`` mark, which no gate selects.

A module that skips itself at import (``pytest.importorskip``,
``allow_module_level``) skips *before* ``-m`` selection, so it is reported
whatever its marks — a default-suite module included. That fails here too,
deliberately: fail closed rather than let an integration module vanish unseen.

    python scripts/check_no_skips.py <report.xml>

Exit codes: 0 at least one test ran and none skipped; 1 a skipped or xfailed
test (each listed with its reason), or no tests at all; 2 the report is
missing, unparsable, or not given. Stdlib only.
"""

import sys
import xml.etree.ElementTree as ET

# What pytest writes as the message of a module-level skip; the reason is in
# the element's text, as the repr of a (path, line, "Skipped: why") tuple.
COLLECTION_SKIP = "collection skipped"


def _describe(case: ET.Element, skip: ET.Element) -> tuple[str, bool]:
    """Return ``(report line, is_collection_skip)`` for one skipped testcase."""
    classname, name = case.get("classname") or "", case.get("name") or ""
    message = skip.get("message", "")
    if message == COLLECTION_SKIP:
        reason = (skip.text or "").strip() or message
        return f"SKIPPED {name} (collection): {reason}", True
    label = "XFAIL" if skip.get("type") == "pytest.xfail" else "SKIPPED"
    test_id = f"{classname}::{name}" if classname else name
    return f"{label} {test_id}: {message}", False


def main(argv: list[str]) -> int:
    """Check the report named by ``argv[0]``; return the process exit code."""
    if len(argv) != 1:
        print("usage: check_no_skips.py <junit-report.xml>", file=sys.stderr)
        return 2
    path = argv[0]
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"ERROR: cannot read JUnit report {path}: {exc}", file=sys.stderr)
        return 2

    cases = list(root.iter("testcase"))
    if not cases:
        print(f"FAIL: no tests ran ({path} holds no testcases)")
        return 1

    skipped = [
        _describe(case, skip) for case in cases if (skip := case.find("skipped")) is not None
    ]
    for line, _ in skipped:
        print(line)
    if not skipped:
        print(f"OK: {len(cases)} tests ran, none skipped")
        return 0
    print(
        f"FAIL: {len(skipped)} of {len(cases)} tests skipped or xfailed. A gate test must "
        "run here; one that needs an external service belongs under the `live` mark."
    )
    if any(collection for _, collection in skipped):
        print(
            "      A (collection) skip is a whole module skipping at import, before marker "
            "selection — whatever its marks. Make it import cleanly, or skip per test."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
