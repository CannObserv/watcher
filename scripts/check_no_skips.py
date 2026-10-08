"""Fail a pytest run that skipped anything, read from its JUnit XML report (#353).

A test that skips itself reports green while verifying nothing, so a gate that
only fails on failures can claim "runs in CI" for a test that never ran. CI's
``integration`` job and ``scripts/pre-ship.sh`` run this after
``pytest -m integration --junitxml=<report>``. A test that genuinely needs an
external service belongs under the ``live`` mark, which no gate selects.

    python scripts/check_no_skips.py <report.xml>

Exit codes: 0 at least one test ran and none skipped; 1 a skipped or xfailed
test (each listed with its reason), or no tests at all; 2 the report is
missing, unparsable, or not given. Stdlib only.
"""

import sys
import xml.etree.ElementTree as ET


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
        (f"{case.get('classname')}::{case.get('name')}", skip.get("message", ""))
        for case in cases
        if (skip := case.find("skipped")) is not None
    ]
    for test_id, reason in skipped:
        print(f"SKIPPED {test_id}: {reason}")
    if skipped:
        print(
            f"FAIL: {len(skipped)} of {len(cases)} tests skipped. A gate test must run here; "
            "one that needs an external service belongs under the `live` mark."
        )
        return 1
    print(f"OK: {len(cases)} tests ran, none skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
