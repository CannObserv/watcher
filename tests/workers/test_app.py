"""Tests for procrastinate App setup and task registration."""

import pytest

from src.workers import get_app, get_conninfo, reset_app


class TestGetConninfo:
    """The worker's DSN rule, shared with the backlog prune (#296 CR 20) so the
    two cannot drift: an operator tool that read the rule differently would
    connect somewhere the worker never would."""

    def test_the_procrastinate_url_wins(self):
        environ = {
            "PROCRASTINATE_DATABASE_URL": "postgresql://a/one",
            "DATABASE_URL": "postgresql+asyncpg://b/two",
        }
        assert get_conninfo(environ) == "postgresql://a/one"

    def test_the_asyncpg_driver_is_stripped(self):
        environ = {"DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/watcher"}
        assert get_conninfo(environ) == "postgresql://u:p@localhost:5432/watcher"

    def test_a_plain_libpq_url_passes_through(self):
        assert get_conninfo({"DATABASE_URL": "postgresql://h/db"}) == "postgresql://h/db"

    @pytest.mark.parametrize("url", ["", "mysql://u:p@localhost/watcher"])
    def test_anything_else_is_refused(self, url):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            get_conninfo({"DATABASE_URL": url})


class TestGetAppRegistration:
    """Verify get_app() produces an App with all tasks and periodic config."""

    def setup_method(self):
        reset_app()

    def teardown_method(self):
        reset_app()

    def test_blueprint_tasks_registered(self):
        app = get_app()
        assert "check_watched_item" in app.tasks
        assert "schedule_tick" in app.tasks

    def test_schedule_tick_registered_as_periodic(self):
        app = get_app()
        assert app.periodic_registry.periodic_tasks, (
            "schedule_tick must be registered as a periodic task"
        )
        task_keys = {name for name, _ in app.periodic_registry.periodic_tasks}
        assert "schedule_tick" in task_keys
