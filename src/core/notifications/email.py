"""Email notification channel — send change alerts via SMTP."""

from email.message import EmailMessage

import aiosmtplib
from pydantic import BaseModel, EmailStr

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


class EmailConfig(BaseModel):
    """Pydantic config model for EmailChannel."""

    host: str
    port: int
    from_addr: EmailStr
    to_addr: EmailStr
    username: str | None = None
    password: str | None = None
    start_tls: bool = True


class EmailChannel:
    """Deliver change notifications as plain-text emails via SMTP."""

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """Send an email using *config* SMTP settings. Return True on success.

        Required config keys: host, port, from_addr, to_addr.
        Optional: username, password, start_tls (default True for STARTTLS on port 587).
        """
        cfg = EmailConfig.model_validate(config)

        msg = EmailMessage()
        msg["Subject"] = f"[watcher] {event.watch_name}: change detected"
        msg["From"] = cfg.from_addr
        msg["To"] = cfg.to_addr
        msg.set_content(event.summary)

        try:
            await aiosmtplib.send(
                msg,
                hostname=cfg.host,
                port=cfg.port,
                username=cfg.username,
                password=cfg.password,
                start_tls=cfg.start_tls,
            )
            return True
        except (aiosmtplib.SMTPException, OSError) as exc:
            logger.warning("email_send_error", extra={"error": str(exc)})
            return False
