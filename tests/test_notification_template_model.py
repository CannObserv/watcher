"""Tests for NotificationTemplate, WatchNcRef, DomainNcRef models and rename."""


def test_imports():
    from src.core.models import (
        DomainNcRef,
        NotificationTemplate,
        WatchNcRef,
        WatchNotificationConfig,
    )

    assert WatchNotificationConfig.__tablename__ == "watch_notification_configs"
    assert NotificationTemplate.__tablename__ == "notification_templates"
    assert WatchNcRef.__tablename__ == "watch_nc_refs"
    assert DomainNcRef.__tablename__ == "domain_nc_refs"
