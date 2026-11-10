"""Guard: the headless jump's retired tables and columns stay dropped (#380).

Design (``docs/plans/2026-10-14-headless-watcher-and-cohort-boundaries-design.md``,
*Deleted*): Archiver owns the operator surface, so the tables and columns
only that surface wrote go. #374 dropped ``fetch_policy_tombstones`` and #376
``last_error_notified_at``; #380 dropped the rest. A model that maps one of
them again re-creates it in the test schema and fails CI's ``alembic check``
against the migrated one, but only after someone writes code against it.
This catches it at the model.

Also gone with the columns: the state no producer reaches any more
(``WatchHealthStatus.PROBING``, ``CheckRefusal.ARCHIVED``), and the audit
event types no code emits. Rows already in ``audit_log`` keep their strings,
the #200 precedent.

Kept apart from ``tests/test_operator_api_retired.py`` and
``tests/test_notification_subsystem_retired.py``: a different surface,
retired by a different issue.
"""

import importlib.util

import pytest

from src.core import domains as core_domains
from src.core.check_now import CheckRefusal
from src.core.models import Base, Domain
from src.core.models import audit_log as audit_log_module
from src.core.models.audit_log import EventType
from src.core.models.watched_item import WatchHealthStatus

_DROPPED_TABLES = (
    "notification_templates",
    "temporal_profiles",
    "api_keys",
    "app_users",
    "fetch_policy_tombstones",
)
_DROPPED_COLUMNS = {
    "watched_items": (
        "name",
        "description",
        "default_tags",
        "default_schedule_config",
        "domain_default_schedule_config",
        "throttle_floor_interval",
        "last_reviewed_at",
        "last_error_notified_at",
        "archived_at",
    ),
    "domains": ("min_interval", "default_schedule_config", "notes", "archived_at"),
}
# Modules that existed only to map or fill the dropped schema.
_RETIRED_MODULES = (
    "src.core.models.notification_template",
    "src.core.models.temporal_profile",
    "src.core.models.api_key",
    "src.core.models.app_user",
    "src.core.watched_items",
    "src.ops.export_for_archiver",
)
# No code emits these since #377/#378/#379, and #380 removed the last emitter
# (the PROBING branch's WATCHED_ITEM_UPDATED).
_RETIRED_EVENT_TYPES = (
    "NOTIFICATION_DISPATCHED",
    "NOTIFICATION_TEST",
    "NOTIFICATION_TEMPLATE_CREATED",
    "NOTIFICATION_TEMPLATE_UPDATED",
    "NOTIFICATION_TEMPLATE_DELETED",
    "NOTIFICATION_TEMPLATE_TESTED",
    "PROFILE_CREATED",
    "PROFILE_UPDATED",
    "PROFILE_DELETED",
    "DOMAIN_CREATED",
    "DOMAIN_UPDATED",
    "DOMAIN_ARCHIVED",
    "DOMAIN_RESTORED",
    "DOMAIN_DELETED",
    "WATCHED_ITEM_THROTTLED",
    "WATCHED_ITEM_UPDATED",
    "WATCHED_ITEM_ARCHIVED",
    "WATCHED_ITEM_RESTORED",
    "WATCHED_ITEM_DELETED",
    "WATCHED_ITEM_PAUSED",
    "WATCHED_ITEM_RESUMED",
    "WATCHED_ITEM_REVIEWED",
)


@pytest.mark.parametrize("table", _DROPPED_TABLES)
def test_no_model_maps_a_dropped_table(table):
    assert table not in Base.metadata.tables


@pytest.mark.parametrize(
    ("table", "column"),
    [(table, column) for table, columns in _DROPPED_COLUMNS.items() for column in columns],
)
def test_no_model_maps_a_dropped_column(table, column):
    assert column not in Base.metadata.tables[table].columns


@pytest.mark.parametrize("module", _RETIRED_MODULES)
def test_retired_module_is_gone(module):
    assert importlib.util.find_spec(module) is None


@pytest.mark.parametrize("name", _RETIRED_EVENT_TYPES)
def test_retired_event_type_is_gone(name):
    assert not hasattr(EventType, name)


def test_no_probing_state():
    """Nothing enters PROBING since #377 deleted ``resolve_watch_target``."""
    assert "PROBING" not in WatchHealthStatus.__members__


def test_no_archived_refusal():
    """Nothing archives an item, so check-now has no archived refusal."""
    assert "ARCHIVED" not in CheckRefusal.__members__


def test_domain_has_no_archive_state():
    """Suspension is ``is_active`` alone; the dashboard's status badge went with it."""
    assert not hasattr(Domain, "status")
    assert not hasattr(core_domains, "DomainArchived")
    assert not hasattr(core_domains, "DEFAULT_MIN_INTERVAL")


def test_audit_module_still_loads():
    """The retired constants went; the model and ``audit()`` stay."""
    assert hasattr(audit_log_module, "audit")
