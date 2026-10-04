"""Central messaging service.

    MessageService
        ├── SMSProvider
        ├── WhatsAppProvider
        └── EmailProvider

Campaigns and the inbox call message_service.send(...) and never talk to a
provider directly.
"""
import logging
import re
from datetime import timedelta

from flask import current_app, render_template
from itsdangerous import URLSafeSerializer
from markupsafe import Markup, escape
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.messaging.base import Attachment, SendResult
from app.messaging.email import ConsoleEmailProvider, OutboundEmail, SMTPEmailProvider
from app.messaging.sms import AfricasTalkingSMSProvider, ConsoleSMSProvider
from app.messaging.whatsapp import ConsoleWhatsAppProvider, MetaWhatsAppProvider
from app.models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    Client,
    ClientStatus,
    ContactChannel,
    Conversation,
    ConversationStatus,
    Message,
    MessageStatus,
    WebhookEvent,
)
from app.utils import normalize_email, normalize_phone, truncate, utcnow

log = logging.getLogger(__name__)

SMS_PROVIDERS = {"console": ConsoleSMSProvider, "africastalking": AfricasTalkingSMSProvider}
WHATSAPP_PROVIDERS = {"console": ConsoleWhatsAppProvider, "meta": MetaWhatsAppProvider}
EMAIL_PROVIDERS = {"console": ConsoleEmailProvider, "smtp": SMTPEmailProvider}

OPT_OUT_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "OPT-OUT"}
CAMPAIGN_REPLY_WINDOW = timedelta(days=14)

UNSUBSCRIBE_SALT = "email-unsubscribe"
TRACKING_SALT = "email-open"


def _signer(salt):
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=salt)


def unsubscribe_token(client_id):
    return _signer(UNSUBSCRIBE_SALT).dumps({"c": client_id})


def tracking_token(message_id):
    return _signer(TRACKING_SALT).dumps({"m": message_id})


def load_token(token, salt):
    try:
        return _signer(salt).loads(token)
    except Exception:  # noqa: BLE001 - any bad token is simply rejected
        return None


def html_to_text(html):
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", html or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class MessageService:
    def __init__(self):
        self._overrides = {}

    # ---- provider selection -------------------------------------------------
    def set_provider(self, channel, provider):
        """Override a provider (used by tests)."""
        self._overrides[channel] = provider

    def clear_overrides(self):
        self._overrides.clear()

    def provider_for(self, channel):
        if channel in self._overrides:
            return self._overrides[channel]
        cfg = current_app.config
        if channel == CHANNEL_SMS:
            return SMS_PROVIDERS.get(cfg["SMS_PROVIDER"], ConsoleSMSProvider)(cfg)
        if channel == CHANNEL_WHATSAPP:
            return WHATSAPP_PROVIDERS.get(cfg["WHATSAPP_PROVIDER"], ConsoleWhatsAppProvider)(cfg)
        if channel == CHANNEL_EMAIL:
            return EMAIL_PROVIDERS.get(cfg["MAIL_PROVIDER"], ConsoleEmailProvider)(cfg)
        raise ValueError(f"Unknown channel: {channel}")

    # ---- outbound -------------------------------------------------------------
    def send(self, message, attachments=None, whatsapp_template=None):
        """Send an outbound Message through its channel's provider and record the outcome.

        The caller commits. Retryable failures leave the message queued (up to
        MESSAGE_MAX_RETRIES) so the caller can try again later.
        """
        provider = self.provider_for(message.channel)
        message.provider = provider.name
        message.status = MessageStatus.SENDING
        db.session.flush()

        try:
            if message.channel == CHANNEL_SMS:
                result = provider.send_message(message.recipient, message.content)
            elif message.channel == CHANNEL_WHATSAPP:
                if whatsapp_template:
                    result = provider.send_template(
                        message.recipient, whatsapp_template["name"],
                        whatsapp_template.get("language", "en"), whatsapp_template.get("params"),
                    )
                else:
                    result = provider.send_message(message.recipient, message.content)
            else:
                result = provider.send_email(self._build_email(message, attachments))
        except Exception as exc:  # noqa: BLE001 - provider bugs must not crash a whole campaign
            log.exception("Provider %s raised while sending message %s", provider.name, message.id)
            result = SendResult(success=False, status="failed", error=str(exc), retryable=True)

        self._apply_send_result(message, result)
        return result

    def _apply_send_result(self, message, result):
        now = utcnow()
        if result.success:
            message.status = MessageStatus.SENT
            message.provider_message_id = result.provider_message_id
            message.sent_at = now
            message.error_message = None
        elif result.retryable and message.retry_count < current_app.config["MESSAGE_MAX_RETRIES"]:
            message.status = MessageStatus.QUEUED
            message.retry_count += 1
            message.error_message = result.error
        else:
            message.status = MessageStatus.FAILED
            message.failed_at = now
            message.error_message = result.error or "Unknown error"
            from app import audit
            audit.record("message.failed", "message", message.id, {"error": message.error_message})

        conversation = message.conversation
        if conversation is not None:
            conversation.last_message_at = now
            conversation.last_message_preview = truncate(message.subject or message.content, 200)
            conversation.last_message_direction = DIRECTION_OUTBOUND
            if result.success and conversation.client is not None:
                conversation.client.last_contacted_at = now

    def _build_email(self, message, attachments=None):
        base = current_app.config["APP_BASE_URL"]
        client_id = message.conversation.client_id if message.conversation else None
        unsubscribe_url = f"{base}/unsubscribe/{unsubscribe_token(client_id)}" if client_id else None
        tracking_url = f"{base}/t/o/{tracking_token(message.id)}.gif" if message.id else None
        body_html = Markup(message.content or "") if message.is_html else escape(message.content or "").replace(
            "\n", Markup("<br>")
        )
        html = render_template(
            "email/campaign.html", subject=message.subject, body_html=body_html,
            unsubscribe_url=unsubscribe_url, tracking_url=tracking_url,
        )
        text = html_to_text(message.content) if message.is_html else (message.content or "")
        if unsubscribe_url:
            text += f"\n\n--\nUnsubscribe: {unsubscribe_url}"
        return OutboundEmail(
            to=message.recipient,
            subject=message.subject or "Allied Tours & Travel",
            html=html,
            text=text,
            attachments=[a if isinstance(a, Attachment) else Attachment(*a) for a in (attachments or [])],
        )

    def send_transactional_email(self, to, subject, html, text):
        """System email (e.g. password reset) — not tied to a client conversation."""
        provider = self.provider_for(CHANNEL_EMAIL)
        result = provider.send_email(OutboundEmail(to=to, subject=subject, html=html, text=text))
        if not result.success:
            log.error("Transactional email to %s failed: %s", to, result.error)
        return result

    # ---- conversations ----------------------------------------------------------
    @staticmethod
    def get_or_create_conversation(client, channel):
        conversation = Conversation.query.filter_by(client_id=client.id, channel=channel).first()
        if conversation is None:
            conversation = Conversation(client=client, channel=channel, status=ConversationStatus.OPEN)
            db.session.add(conversation)
            db.session.flush()
        return conversation

    # ---- inbound & status updates ----------------------------------------------
    def find_client_by_address(self, channel, address):
        if channel == CHANNEL_EMAIL:
            value = normalize_email(address)
        else:
            value = normalize_phone(address) or address
        if not value:
            return None, None
        cc = (
            ContactChannel.query.filter_by(channel=channel, address=value)
            .join(Client).filter(Client.status != ClientStatus.ARCHIVED).first()
        )
        if cc:
            return cc.client, value
        column = Client.email if channel == CHANNEL_EMAIL else Client.phone
        client = Client.query.filter(column == value).order_by(Client.id).first()
        return client, value

    def handle_inbound(self, inbound):
        """Store an inbound message. Returns the Message, or None if it was already stored."""
        if inbound.provider_message_id:
            existing = Message.query.filter_by(
                channel=inbound.channel, provider_message_id=inbound.provider_message_id
            ).first()
            if existing:
                return None

        client, address = self.find_client_by_address(inbound.channel, inbound.sender)
        if client is None:
            client = self._create_prospect(inbound, address)

        conversation = self.get_or_create_conversation(client, inbound.channel)
        if conversation.status in (ConversationStatus.RESOLVED, ConversationStatus.CLOSED):
            conversation.status = ConversationStatus.OPEN

        received_at = inbound.received_at or utcnow()
        campaign_id = self._attribute_campaign(conversation, received_at)
        message = Message(
            conversation=conversation,
            campaign_id=campaign_id,
            channel=inbound.channel,
            direction=DIRECTION_INBOUND,
            sender=address or inbound.sender,
            recipient=inbound.recipient,
            subject=inbound.subject,
            content=inbound.content,
            provider_message_id=inbound.provider_message_id,
            status=MessageStatus.RECEIVED,
            created_at=received_at,
        )
        db.session.add(message)

        conversation.last_message_at = received_at
        conversation.last_message_preview = truncate(inbound.subject or inbound.content, 200)
        conversation.last_message_direction = DIRECTION_INBOUND
        conversation.unread_count = (conversation.unread_count or 0) + 1
        if inbound.subject and inbound.channel == CHANNEL_EMAIL:
            conversation.subject = inbound.subject
        client.last_contacted_at = received_at

        if inbound.channel in (CHANNEL_SMS, CHANNEL_WHATSAPP) and self._is_opt_out(inbound.content):
            self._opt_out(client, inbound.channel, address, source="keyword")

        db.session.flush()
        return message

    @staticmethod
    def _is_opt_out(text):
        return (text or "").strip().upper().rstrip(".!") in OPT_OUT_KEYWORDS

    @staticmethod
    def _opt_out(client, channel, address, source):
        cc = client.channel(channel)
        if cc is None:
            cc = ContactChannel(client=client, channel=channel, address=address)
            db.session.add(cc)
        cc.opt_out(source)
        from app import audit
        audit.record("client.opted_out", "client", client.id, {"channel": channel, "source": source})

    @staticmethod
    def _create_prospect(inbound, address):
        client = Client(
            full_name=inbound.sender_name or address or inbound.sender,
            status=ClientStatus.PROSPECT,
            notes=f"Created automatically from an inbound {inbound.channel} message.",
        )
        if inbound.channel == CHANNEL_EMAIL:
            client.email = address
        else:
            client.phone = address
        db.session.add(client)
        db.session.add(ContactChannel(client=client, channel=inbound.channel, address=address))
        if inbound.channel == CHANNEL_WHATSAPP and address:
            db.session.add(ContactChannel(client=client, channel=CHANNEL_SMS, address=address))
        db.session.flush()
        return client

    @staticmethod
    def _attribute_campaign(conversation, received_at):
        """Link a reply to the campaign whose message the client most recently received."""
        last_campaign_msg = (
            Message.query.filter(
                Message.conversation_id == conversation.id,
                Message.direction == DIRECTION_OUTBOUND,
                Message.campaign_id.isnot(None),
                Message.created_at >= received_at - CAMPAIGN_REPLY_WINDOW,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        return last_campaign_msg.campaign_id if last_campaign_msg else None

    def handle_status(self, update):
        """Apply a delivery status update. Returns the Message if it changed."""
        message = Message.query.filter_by(
            channel=update.channel, provider_message_id=update.provider_message_id
        ).first()
        if message is None or message.direction != DIRECTION_OUTBOUND:
            return None
        return self.apply_status(message, update.status, update.occurred_at, update.error)

    @staticmethod
    def apply_status(message, status, occurred_at=None, error=None):
        when = occurred_at or utcnow()
        current = message.status
        if status == MessageStatus.FAILED:
            # A failure report after delivery/read is stale; ignore it.
            if current in (MessageStatus.FAILED, MessageStatus.DELIVERED, MessageStatus.READ):
                return None
            message.status = MessageStatus.FAILED
            message.failed_at = when
            message.error_message = error or message.error_message or "Delivery failed"
            return message

        new_rank = MessageStatus.RANK.get(status)
        if new_rank is None:
            return None
        # Timestamps are recorded even when the status itself doesn't move
        # (e.g. "delivered" arriving after "read").
        changed = False
        if status == MessageStatus.SENT and not message.sent_at:
            message.sent_at, changed = when, True
        if status in (MessageStatus.DELIVERED, MessageStatus.READ) and not message.delivered_at:
            message.delivered_at, changed = when, True
        if status == MessageStatus.READ and not message.read_at:
            message.read_at, changed = when, True
        if current != MessageStatus.FAILED and new_rank > MessageStatus.RANK.get(current, -1):
            message.status, changed = status, True
        return message if changed else None

    # ---- webhooks ---------------------------------------------------------------
    def record_webhook(self, channel, provider_name, result, payload):
        """Persist a webhook event and process it exactly once.

        Returns (event, duplicate).
        """
        existing = WebhookEvent.query.filter_by(channel=channel, event_key=result.event_key).first()
        if existing is not None:
            return existing, True

        event = WebhookEvent(
            channel=channel, provider=provider_name, event_key=result.event_key,
            event_type=result.event_type, payload=payload,
        )
        db.session.add(event)
        try:
            db.session.flush()
        except IntegrityError:
            # A concurrent delivery of the same webhook won the race.
            db.session.rollback()
            return WebhookEvent.query.filter_by(channel=channel, event_key=result.event_key).first(), True

        try:
            with db.session.begin_nested():
                stored = [self.handle_inbound(m) for m in result.inbound]
                updated = [self.handle_status(s) for s in result.statuses]
            event.status = (
                WebhookEvent.STATUS_PROCESSED if (result.inbound or result.statuses) else WebhookEvent.STATUS_IGNORED
            )
            event.processed_at = utcnow()
            log.info("Webhook %s/%s: %d inbound stored, %d statuses applied", channel, result.event_type,
                     len([m for m in stored if m]), len([u for u in updated if u]))
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed processing %s webhook", channel)
            event.status = WebhookEvent.STATUS_FAILED
            event.error_message = str(exc)
        db.session.commit()
        return event, False


message_service = MessageService()
