"""Tests for procrastinate App setup and task registration."""

from src.workers import get_app, reset_app


class TestGetAppRegistration:
    """Verify get_app() produces an App with all tasks and periodic config."""

    def setup_method(self):
        reset_app()

    def teardown_method(self):
        reset_app()

    def test_blueprint_tasks_registered(self):
        app = get_app()
        assert "check_watch" in app.tasks
        assert "schedule_tick" in app.tasks

    def test_schedule_tick_registered_as_periodic(self):
        app = get_app()
        assert app.periodic_registry.periodic_tasks, (
            "schedule_tick must be registered as a periodic task"
        )
        task_keys = {name for name, _ in app.periodic_registry.periodic_tasks}
        assert "schedule_tick" in task_keys
