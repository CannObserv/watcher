"""Run the dark-mode.js behavior tests inside the pytest suite.

The three-state color-scheme toggle (light / system / dark) lives in vanilla JS;
the dashboard has no JS test harness, so the logic is exercised against a minimal
DOM/localStorage/matchMedia stub via Node's built-in runner (``node --test``).
Wrapping it in a pytest test means the behavior is covered by pre-ship, not just
manual checks. Skipped when ``node`` is unavailable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_JS_TEST = Path(__file__).parent / "js" / "dark-mode.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dark_mode_js_behavior():
    result = subprocess.run(
        ["node", "--test", str(_JS_TEST)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
