"""Run the focus-trap.js behavior tests inside the pytest suite (#39).

The dashboard has no JS test harness, so the trap logic is exercised against a
minimal DOM stub via Node's built-in runner (``node --test``). Wrapping it in a
pytest test means the behavior is covered by pre-ship, not just manual checks.
Skipped when ``node`` is unavailable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_JS_TEST = Path(__file__).parent / "js" / "focus-trap.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_focus_trap_js_behavior():
    result = subprocess.run(
        ["node", "--test", str(_JS_TEST)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
