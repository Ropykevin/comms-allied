"""WhatsApp via the official WhatsApp Business Platform (Meta Cloud API).

No WhatsApp Web automation or unofficial clients are used.
"""
import json
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import requests

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


class WhatsAppProvider(ABC):
    name = "whatsapp"

    def __init__(self, config):
        self.config = config

    @abstractmethod
    def send_message(self, to: str, body: str) -> SendResult:
        ...

    @abstractmethod
    def send_template(self, to: str, template_name: str, language: str = "en", params=None) -> SendResult:
        ...

    @abstractmethod
    def check_status(self, provider_message_id: str):
        ...

    def verify_subscription(self, args):
        """Handle Meta's GET verification handshake. Returns the challenge or None."""
        expected = self.config.get("WHATSAPP_VERIFY_TOKEN")
        if args.get("hub.mode") == "subscribe" and constant_time_equals(args.get("hub.verify_token"), expected):
            return args.get("hub.challenge", "")
        return None

    def verify_webhook(self, request):
        """Meta signs each POST with HMAC-SHA256 of the raw body using the app secret."""
        secret = self.config.get("WHATSAPP_APP_SECRET")
        if not secret:
            raise WebhookVerificationError("WHATSAPP_APP_SECRET is not configured")
        header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac_sha256_hex(secret, request.get_data())
        if not constant_time_equals(header, expected):
            raise WebhookVerificationError("Invalid WhatsApp webhook signature")

    def process_webhook(self, request) -> WebhookResult:
        raw = request.get_data()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        result = WebhookResult(event_key=f"wa:{body_digest(raw)}", event_type="unknown")

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    result.inbound.append(
                        InboundMessage(
                            channel="whatsapp",
                            sender="+" + msg.get("from", "").lstrip("+"),
                            recipient=(value.get("metadata") or {}).get("display_phone_number"),
                            content=_message_text(msg),
                            provider_message_id=msg.get("id"),
                            sender_name=names.get(msg.get("from")),
                            received_at=_ts(msg.get("timestamp")),
                        )
                    )
                for st in value.get("statuses", []):
                    errors = st.get("errors") or []
                    error = None
                    if errors:
                        e = errors[0]
                        error = e.get("message") or e.get("title") or f"Error {e.get('code')}"
                    status = st.get("status")
                    if status not in ("sent", "delivered", "read", "failed"):
                        continue
                    result.statuses.append(
                        StatusUpdate(
                            channel="whatsapp",
                            provider_message_id=st.get("id"),
                            status=status,
                            occurred_at=_ts(st.get("timestamp")),
                            error=error,
                        )
                    )
        if result.inbound and result.statuses:
            result.event_type = "mixed"
        elif result.inbound:
            result.event_type = "inbound"
        elif result.statuses:
            result.event_type = "status"
        return result


def _message_text(msg):
    kind = msg.get("type")
    if kind == "text":
        return (msg.get("text") or {}).get("body", "")
    if kind == "button":
        return (msg.get("button") or {}).get("text", "")
    if kind == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title", "")
    if kind in ("image", "video", "document", "audio", "sticker"):
        caption = (msg.get(kind) or {}).get("caption")
        return f"[{kind} received]" + (f" {caption}" if caption else "")
    if kind == "location":
        loc = msg.get("location") or {}
        return f"[location] {loc.get('latitude')}, {loc.get('longitude')}"
    return f"[{kind or 'unsupported'} message]"


def _ts(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


class ConsoleWhatsAppProvider(WhatsAppProvider):
    name = "console-whatsapp"

    def send_message(self, to, body):
        message_id = f"wamid.console-{uuid.uuid4().hex}"
        log.info("[WhatsApp] to=%s id=%s body=%r", to, message_id, body)
        return SendResult(success=True, provider_message_id=message_id, status="sent")

    def send_template(self, to, template_name, language="en", params=None):
        message_id = f"wamid.console-{uuid.uuid4().hex}"
        log.info("[WhatsApp template] to=%s template=%s params=%r", to, template_name, params)
        return SendResult(success=True, provider_message_id=message_id, status="sent")

    def check_status(self, provider_message_id):
        return None


class MetaWhatsAppProvider(WhatsAppProvider):
    name = "meta-whatsapp"
    RETRYABLE_CODES = {4, 80007, 130429, 131016, 131048, 131056}

    def _endpoint(self):
        return (
            f"https://graph.facebook.com/{self.config['WHATSAPP_API_VERSION']}/"
            f"{self.config['WHATSAPP_PHONE_NUMBER_ID']}/messages"
        )

    def _post(self, payload):
        if not self.config.get("WHATSAPP_API_KEY") or not self.config.get("WHATSAPP_PHONE_NUMBER_ID"):
            return SendResult(success=False, status="failed", error="WhatsApp credentials are not configured")
        try:
            resp = requests.post(
                self._endpoint(),
                json=payload,
                headers={"Authorization": f"Bearer {self.config['WHATSAPP_API_KEY']}"},
                timeout=15,
            )
        except requests.RequestException as exc:
            return SendResult(success=False, status="failed", error=f"Network error: {exc}", retryable=True)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.ok and data.get("messages"):
            return SendResult(success=True, provider_message_id=data["messages"][0].get("id"), status="sent", raw=data)
        error = data.get("error") or {}
        retryable = resp.status_code >= 500 or resp.status_code == 429 or error.get("code") in self.RETRYABLE_CODES
        return SendResult(
            success=False,
            status="failed",
            error=error.get("message") or f"WhatsApp API error {resp.status_code}",
            retryable=retryable,
            raw=data,
        )

    @staticmethod
    def _to(number):
        return (number or "").lstrip("+")

    def send_message(self, to, body):
        return self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self._to(to),
            "type": "text",
            "text": {"preview_url": True, "body": body},
        })

    def send_template(self, to, template_name, language="en", params=None):
        template = {"name": template_name, "language": {"code": language}}
        if params:
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(p) or "-"} for p in params],
            }]
        return self._post({
            "messaging_product": "whatsapp",
            "to": self._to(to),
            "type": "template",
            "template": template,
        })

    def check_status(self, provider_message_id):
        # The Cloud API reports status only through webhooks.
        return None
