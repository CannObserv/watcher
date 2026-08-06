"""Tests that dashboard modules do not import from worker internals."""

from pathlib import Path


class TestDashboardDecoupling:
    def test_dashboard_routes_does_not_import_workers(self):
        """No dashboard route module may import from src.workers.*."""
        route_files = sorted(Path("src/dashboard/routes").glob("*.py"))
        assert route_files, "dashboard routes package not found"
        for path in route_files:
            assert "from src.workers" not in path.read_text(), path
