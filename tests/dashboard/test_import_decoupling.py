"""Tests that dashboard modules do not import from worker internals."""

from pathlib import Path

from src.core.rate_limiter import get_rate_limiter, reset_rate_limiter


class TestDashboardDecoupling:
    def test_dashboard_routes_does_not_import_workers(self):
        """No dashboard route module may import from src.workers.*."""
        route_files = sorted(Path("src/dashboard/routes").glob("*.py"))
        assert route_files, "dashboard routes package not found"
        for path in route_files:
            assert "from src.workers" not in path.read_text(), path

    def test_get_rate_limiter_importable_from_core(self):
        """get_rate_limiter must be importable from src.core.rate_limiter."""
        reset_rate_limiter()
        limiter = get_rate_limiter()
        assert limiter is not None
        reset_rate_limiter()
