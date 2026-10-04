import json
import logging
import mimetypes
import smtplib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Optional

from app.messaging.base import (
    InboundMessage,
    SendResult,
    StatusUpdate,
    WebhookResult,
    WebhookVerificationError,
    body_digest,
    constant_time_equals,
    hmac_sha256_hex,
)

log = logging.getLogger(__name__)


@dataclass
class OutboundEmail:
    to: str
    subject: str
    html: str
    text: str
    sender: Optional[str] = None
    reply_to: Optional[str] = None
    attachments: list = field(default_factory=list)


class EmailProvider(ABC):
    name = "email"

    EVENT_STATUS = {
        "sent": "sent",
        "delivered": "delivered",
        "opened": "read",
        "open": "read",
        "bounced": "failed",
        "bounce": "failed",
        "failed": "failed",
        "dropped": "failed",
        "complained": "failed",
    }

    def __init__(self, config):
        self.config = config

    @abstractmethod
    def send_email(self, email: OutboundEmail) -> SendResult:
        ...

    def verify_webhook(self, request):
        """Inbound relays sign the raw JSON body: X-Allied-Signature: <hex HMAC-SHA256>."""
        secret = self.config.get("EMAIL_WEBHOOK_SECRET")
        if not secret:
            raise WebhookVerificationError("EMAIL_WEBHOOK_SECRET is not configured")
        supplied = request.headers.get("X-Allied-Signature", "")
        if not constant_time_equals(supplied, hmac_sha256_hex(secret, request.get_data())):
            raise WebhookVerificationError("Invalid email webhook signature")

    def parse_webhook(self, request) -> WebhookResult:
        raw = request.get_data()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        events = payload.get("events") if isinstance(payload.get("events"), list) else [payload]
        result = WebhookResult(event_key=f"email:{body_digest(raw)}", event_type="unknown")
        for event in events:
            if (event.get("type") or "").lower() == "inbound":
                result.inbound.extend(self.process_inbound_email(event).inbound)
            else:
                result.statuses.extend(self.process_delivery_event(event).statuses)
        if result.inbound:
            result.event_type = "inbound"
        elif result.statuses:
            result.event_type = "status"
        return result

    def process_inbound_email(self, event) -> WebhookResult:
        name, address = parseaddr(event.get("from", ""))
        provider_id = event.get("id") or event.get("message_id") or f"email-in-{uuid.uuid4().hex}"
        inbound = InboundMessage(
            channel="email",
            sender=address.lower(),
            sender_name=event.get("from_name") or name or None,
            recipient=event.get("to"),
            subject=event.get("subject"),
            content=event.get("text") or event.get("body") or "",
            provider_message_id=provider_id,
        )
        return WebhookResult(event_key=f"in:{provider_id}", event_type="inbound", inbound=[inbound])

    def process_delivery_event(self, event) -> WebhookResult:
        status = self.EVENT_STATUS.get((event.get("type") or "").lower())
        message_id = event.get("message_id")
        result = WebhookResult(event_key=f"status:{message_id}:{event.get('type')}", event_type="status")
        if status and message_id:
            error = event.get("reason")
            if status == "failed" and not error:
                error = (event.get("type") or "failed").title()
            result.statuses.append(
                StatusUpdate(channel="email", provider_message_id=message_id, status=status, error=error)
            )
        return result

    def _build_message(self, email: OutboundEmail, message_id: str):
        msg = EmailMessage()
        msg["Subject"] = email.subject
        msg["From"] = email.sender or self.config["MAIL_DEFAULT_SENDER"]
        msg["To"] = email.to
        msg["Message-ID"] = message_id
        if email.reply_to:
            msg["Reply-To"] = email.reply_to
        msg.set_content(email.text or "")
        msg.add_alternative(email.html or "", subtype="html")
        for att in email.attachments:
            mimetype = att.mimetype or mimetypes.guess_type(att.filename)[0] or "application/octet-stream"
            maintype, subtype = mimetype.split("/", 1)
            with open(att.path, "rb") as fh:
                msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype, filename=att.filename)
        return msg

    def _domain(self):
        _, address = parseaddr(self.config.get("MAIL_DEFAULT_SENDER", ""))
        return address.split("@")[-1] if "@" in address else "alliedtours.local"


class ConsoleEmailProvider(EmailProvider):
    name = "console-email"

    def send_email(self, email):
        message_id = make_msgid(domain=self._domain())
        self._build_message(email, message_id)  # validates headers/attachments
        log.info("[Email] to=%s subject=%r id=%s attachments=%d", email.to, email.subject, message_id,
                 len(email.attachments))
        return SendResult(success=True, provider_message_id=message_id, status="sent")


class SMTPEmailProvider(EmailProvider):
    name = "smtp"

    def send_email(self, email):
        if not self.config.get("MAIL_SERVER"):
            return SendResult(success=False, status="failed", error="MAIL_SERVER is not configured")
        message_id = make_msgid(domain=self._domain())
        msg = self._build_message(email, message_id)
        try:
            smtp_cls = smtplib.SMTP_SSL if self.config.get("MAIL_USE_SSL") else smtplib.SMTP
            with smtp_cls(self.config["MAIL_SERVER"], self.config["MAIL_PORT"], timeout=20) as smtp:
                if self.config.get("MAIL_USE_TLS") and not self.config.get("MAIL_USE_SSL"):
                    smtp.starttls()
                if self.config.get("MAIL_USERNAME"):
                    smtp.login(self.config["MAIL_USERNAME"], self.config["MAIL_PASSWORD"])
                refused = smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            return SendResult(success=False, status="failed", error=f"Recipient refused: {exc.recipients}")
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError) as exc:
            return SendResult(success=False, status="failed", error=f"SMTP connection error: {exc}", retryable=True)
        except smtplib.SMTPException as exc:
            return SendResult(success=False, status="failed", error=f"SMTP error: {exc}")
        if refused:
            return SendResult(success=False, status="failed", error=f"Recipient refused: {refused}")
        return SendResult(success=True, provider_message_id=message_id, status="sent")
