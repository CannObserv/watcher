"""Tests that dashboard modules do not import from worker internals."""

import inspect
import sys


class TestDashboardDecoupling:
    def test_dashboard_routes_does_not_import_workers_tasks(self):
        """dashboard/routes.py must not import from src.workers.tasks."""
        # Remove cached module to get a fresh import
        for key in list(sys.modules.keys()):
            if "src.dashboard.routes" in key:
                del sys.modules[key]

        import src.dashboard.routes as routes_mod

        source = inspect.getsource(routes_mod)
        assert "from src.workers.tasks import" not in source
        assert "from src.workers" not in source

    def test_get_rate_limiter_importable_from_core(self):
        """get_rate_limiter must be importable from src.core.rate_limiter."""
        from src.core.rate_limiter import get_rate_limiter, reset_rate_limiter

        reset_rate_limiter()
        limiter = get_rate_limiter()
        assert limiter is not None
        reset_rate_limiter()
