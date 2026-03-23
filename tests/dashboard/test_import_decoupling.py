"""Tests that dashboard modules do not import from worker internals."""

from pathlib import Path

from src.core.rate_limiter import get_rate_limiter, reset_rate_limiter


class TestDashboardDecoupling:
    def test_dashboard_routes_does_not_import_workers(self):
        """dashboard/routes.py must not import from src.workers.*."""
        source = Path("src/dashboard/routes.py").read_text()
        assert "from src.workers" not in source

    def test_get_rate_limiter_importable_from_core(self):
        """get_rate_limiter must be importable from src.core.rate_limiter."""
        reset_rate_limiter()
        limiter = get_rate_limiter()
        assert limiter is not None
        reset_rate_limiter()
