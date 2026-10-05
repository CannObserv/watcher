"""The renderer stays pure (#222 CR 2).

``content`` and ``preview_fixtures`` render a ``ChangeDiff``; they never load
one. Importing them must not pull in the store side — the DB models, the GCS
SDK, the bus code — which belongs to ``diff_loader`` alone. Checked in a fresh
interpreter: this one has long since imported everything.
"""

import subprocess
import sys

import pytest

IO_MODULES = (
    "src.core.notifications.diff_loader",
    "src.core.blobs",
    "src.core.fetch_commands",
    "src.core.models.process_command",
    "google.cloud.storage",
)


@pytest.mark.parametrize(
    "module",
    [
        "src.core.notifications.content",
        "src.core.notifications.diff",
        "src.core.notifications.preview_fixtures",
    ],
)
def test_rendering_modules_import_no_store_side(module):
    code = f"import sys, {module}; print(','.join(m for m in {IO_MODULES!r} if m in sys.modules))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"{module} loads the store side: {out}"
