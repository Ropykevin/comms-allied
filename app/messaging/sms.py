import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime

import requests

from app.messaging.base import (
    InboundMessage,
    SendResult,
    StatusUpdate,
    WebhookResult,
    WebhookVerificationError,
    body_digest,
    constant_time_equals,
)

log = logging.getLogger(__name__)


class SMSProvider(ABC):
    name = "sms"

    def __init__(self, config):
        self.config = config

    @abstractmethod
    def send_message(self, to: str, body: str) -> SendResult:
        ...

    @abstractmethod
    def check_delivery_status(self, provider_message_id: str):
        """Return a StatusUpdate, or None if the provider only reports status via webhooks."""

    @abstractmethod
    def process_incoming_message(self, request) -> WebhookResult:
        """Parse an inbound SMS or delivery report webhook."""

    def verify_webhook(self, request):
        """Shared-secret token, passed as ?token=… on the callback URL configured at the provider."""
        expected = self.config.get("SMS_WEBHOOK_TOKEN")
        if not expected:
            raise WebhookVerificationError("SMS_WEBHOOK_TOKEN is not configured")
        supplied = request.args.get("token") or request.headers.get("X-Webhook-Token")
        if not constant_time_equals(supplied, expected):
            raise WebhookVerificationError("Invalid SMS webhook token")


def _parse_sms_payload(channel_provider, request) -> WebhookResult:
    """Both supported providers post simple key/value payloads.

    Inbound:          from, to, text, id, date
    Delivery report:  id, status, failureReason
    """
    data = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    raw = request.get_data()
    if data.get("text") is not None and data.get("from"):
        provider_id = data.get("id") or f"sms-in-{body_digest(raw)[:32]}"
        inbound = InboundMessage(
            channel="sms",
            sender=data["from"],
            recipient=data.get("to"),
            content=data.get("text", ""),
            provider_message_id=provider_id,
            received_at=_parse_date(data.get("date")),
        )
        return WebhookResult(event_key=f"in:{provider_id}", event_type="inbound", inbound=[inbound])

    if data.get("id") and data.get("status"):
        status = channel_provider.map_status(data["status"])
        update = StatusUpdate(
            channel="sms",
            provider_message_id=data["id"],
            status=status,
            error=data.get("failureReason") or None,
        )
        return WebhookResult(
            event_key=f"status:{data['id']}:{data['status']}", event_type="status", statuses=[update]
        )

    return WebhookResult(event_key=f"unknown:{body_digest(raw)}", event_type="unknown")


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


class ConsoleSMSProvider(SMSProvider):
    """Development provider: logs messages instead of sending them."""

    name = "console-sms"

    def send_message(self, to, body):
        message_id = f"console-sms-{uuid.uuid4().hex}"
        log.info("[SMS] to=%s id=%s body=%r", to, message_id, body)
        return SendResult(success=True, provider_message_id=message_id, status="sent")

    def check_delivery_status(self, provider_message_id):
        return None

    def map_status(self, value):
        return AfricasTalkingSMSProvider.STATUS_MAP.get(value, value.lower() if value else "sent")

    def process_incoming_message(self, request):
        return _parse_sms_payload(self, request)


class AfricasTalkingSMSProvider(SMSProvider):
    """Africa's Talking bulk SMS API (widely used in Kenya)."""

    name = "africastalking"

    STATUS_MAP = {
        "Submitted": "sent",
        "Sent": "sent",
        "Buffered": "sent",
        "Success": "delivered",
        "Failed": "failed",
        "Rejected": "failed",
    }

    def map_status(self, value):
        return self.STATUS_MAP.get(value, "sent")

    def send_message(self, to, body):
        if not self.config.get("SMS_API_KEY") or not self.config.get("SMS_USERNAME"):
            return SendResult(success=False, status="failed", error="SMS provider credentials are not configured")
        payload = {"username": self.config["SMS_USERNAME"], "to": to, "message": body}
        if self.config.get("SMS_SENDER_ID"):
            payload["from"] = self.config["SMS_SENDER_ID"]
        try:
            resp = requests.post(
                self.config["SMS_API_URL"],
                data=payload,
                headers={"apiKey": self.config["SMS_API_KEY"], "Accept": "application/json"},
                timeout=15,
            )
        except requests.RequestException as exc:
            return SendResult(success=False, status="failed", error=f"Network error: {exc}", retryable=True)

        if resp.status_code >= 500 or resp.status_code == 429:
            return SendResult(success=False, status="failed", error=f"Provider error {resp.status_code}", retryable=True)
        try:
            data = resp.json()
        except ValueError:
            return SendResult(success=False, status="failed", error=f"Unexpected response ({resp.status_code})")

        recipients = (data.get("SMSMessageData") or {}).get("Recipients") or []
        if not recipients:
            message = (data.get("SMSMessageData") or {}).get("Message") or "No recipients accepted"
            return SendResult(success=False, status="failed", error=message, raw=data)
        first = recipients[0]
        # 100 Processed, 101 Sent, 102 Queued
        if int(first.get("statusCode", 0)) in (100, 101, 102):
            return SendResult(success=True, provider_message_id=first.get("messageId"), status="sent", raw=data)
        return SendResult(success=False, status="failed", error=first.get("status") or "Rejected", raw=data)

    def check_delivery_status(self, provider_message_id):
        # Africa's Talking reports delivery status via callbacks only.
        return None

    def process_incoming_message(self, request):
        return _parse_sms_payload(self, request)
