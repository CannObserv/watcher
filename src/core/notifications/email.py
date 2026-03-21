"""Email notification channel — send change alerts via SMTP."""

from email.message import EmailMessage

import aiosmtplib

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


class EmailChannel:
    """Deliver change notifications as plain-text emails via SMTP."""

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """Send an email using *config* SMTP settings. Return True on success.

        Required config keys: host, port, from_addr, to_addr.
        Optional: username, password, start_tls (default True for STARTTLS on port 587).
        """
        host = config.get("host")
        port = config.get("port")
        from_addr = config.get("from_addr")
        to_addr = config.get("to_addr")

        if not all([host, port, from_addr, to_addr]):
            logger.warning("email_missing_config")
            return False

        msg = EmailMessage()
        msg["Subject"] = f"[watcher] {event.watch_name}: change detected"
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content(event.summary)

        try:
            await aiosmtplib.send(
                msg,
                hostname=host,
                port=port,
                username=config.get("username"),
                password=config.get("password"),
                start_tls=config.get("start_tls", True),
            )
            return True
        except (aiosmtplib.SMTPException, OSError) as exc:
            logger.warning("email_send_error", extra={"error": str(exc)})
            return False
