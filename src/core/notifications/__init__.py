"""Notification channels for change alerts."""

from src.core.notifications.base import ChangeEvent, NotificationChannel
from src.core.notifications.email import EmailChannel
from src.core.notifications.slack import SlackChannel
from src.core.notifications.webhook import WebhookChannel

__all__ = [
    "ChangeEvent",
    "EmailChannel",
    "NotificationChannel",
    "SlackChannel",
    "WebhookChannel",
]
