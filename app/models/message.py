from app.extensions import db
from app.models.client import CHANNEL_LABELS
from app.utils import utcnow


class MessageStatus:
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    RECEIVED = "received"  # inbound messages
    ALL = (QUEUED, SENDING, SENT, DELIVERED, READ, FAILED, RECEIVED)

    # Position in the outbound lifecycle. Webhooks may arrive out of order, so a
    # status update is only applied if it moves the message forward.
    RANK = {QUEUED: 0, SENDING: 1, SENT: 2, DELIVERED: 3, READ: 4}

    LABELS = {
        QUEUED: "Queued", SENDING: "Sending", SENT: "Sent", DELIVERED: "Delivered",
        READ: "Read", FAILED: "Failed", RECEIVED: "Received",
    }


DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id", ondelete="SET NULL"), index=True)
    channel = db.Column(db.String(20), nullable=False)
    direction = db.Column(db.String(10), nullable=False)
    sender = db.Column(db.String(255))
    recipient = db.Column(db.String(255))
    subject = db.Column(db.String(255))
    content = db.Column(db.Text)
    is_html = db.Column(db.Boolean, nullable=False, default=False)
    provider = db.Column(db.String(40))
    provider_message_id = db.Column(db.String(255))
    status = db.Column(db.String(20), nullable=False, default=MessageStatus.QUEUED, index=True)
    sent_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    read_at = db.Column(db.DateTime)
    failed_at = db.Column(db.DateTime)
    error_message = db.Column(db.Text)
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    sent_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    __table_args__ = (
        # Provider ids are unique per channel; this is what makes webhook retries idempotent.
        db.UniqueConstraint("channel", "provider_message_id", name="uq_messages_channel_provider_message_id"),
        db.CheckConstraint("channel IN ('sms', 'whatsapp', 'email')", name="channel_valid"),
        db.CheckConstraint("direction IN ('inbound', 'outbound')", name="direction_valid"),
        db.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'delivered', 'read', 'failed', 'received')",
            name="status_valid",
        ),
        db.Index("ix_messages_campaign_status", "campaign_id", "status"),
    )

    conversation = db.relationship("Conversation", back_populates="messages")
    campaign = db.relationship("Campaign")
    sent_by = db.relationship("User")

    @property
    def is_inbound(self):
        return self.direction == DIRECTION_INBOUND

    @property
    def channel_label(self):
        return CHANNEL_LABELS.get(self.channel, self.channel)

    @property
    def status_label(self):
        return MessageStatus.LABELS.get(self.status, self.status)
