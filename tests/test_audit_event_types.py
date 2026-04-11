"""Test for new audit event types in Task 3."""


def test_new_event_types_exist():
    from src.core.models.audit_log import EventType

    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_CREATED")
    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_UPDATED")
    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_DELETED")
    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_TESTED")
    assert hasattr(EventType, "WATCH_NC_ASSIGNED")
    assert hasattr(EventType, "WATCH_NC_UNASSIGNED")
    assert hasattr(EventType, "DOMAIN_NC_DEFAULT_ADDED")
    assert hasattr(EventType, "DOMAIN_NC_DEFAULT_REMOVED")
