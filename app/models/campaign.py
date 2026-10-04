from app.extensions import db
from app.models.client import CHANNEL_LABELS
from app.utils import utcnow


class CampaignStatus:
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    SENDING = "sending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ALL = (DRAFT, SCHEDULED, QUEUED, SENDING, COMPLETED, FAILED, CANCELLED)
    EDITABLE = (DRAFT, SCHEDULED)
    LABELS = {
        DRAFT: "Draft", SCHEDULED: "Scheduled", QUEUED: "Queued", SENDING: "Sending",
        COMPLETED: "Completed", FAILED: "Failed", CANCELLED: "Cancelled",
    }


class Campaign(db.Model):
    __tablename__ = "campaigns"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    channel = db.Column(db.String(20))
    # {"category_ids": [..], "tag_ids": [..], "statuses": [..], "location": ".."}
    audience = db.Column(db.JSON, nullable=False, default=dict)
    subject = db.Column(db.String(255))
    content = db.Column(db.Text)
    is_html = db.Column(db.Boolean, nullable=False, default=False)
    template_id = db.Column(db.Integer, db.ForeignKey("message_templates.id", ondelete="SET NULL"))
    attachment_path = db.Column(db.String(500))
    attachment_name = db.Column(db.String(255))
    status = db.Column(db.String(20), nullable=False, default=CampaignStatus.DRAFT, index=True)
    scheduled_at = db.Column(db.DateTime, index=True)
    timezone = db.Column(db.String(64), nullable=False, default="Africa/Nairobi")
    total_recipients = db.Column(db.Integer, nullable=False, default=0)
    excluded_count = db.Column(db.Integer, nullable=False, default=0)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    sent_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    queued_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('draft', 'scheduled', 'queued', 'sending', 'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
        db.CheckConstraint("channel IS NULL OR channel IN ('sms', 'whatsapp', 'email')", name="channel_valid"),
    )

    template = db.relationship("MessageTemplate")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    sent_by = db.relationship("User", foreign_keys=[sent_by_id])
    recipients = db.relationship(
        "CampaignRecipient", back_populates="campaign", cascade="all, delete-orphan", lazy="dynamic"
    )

    @property
    def status_label(self):
        return CampaignStatus.LABELS.get(self.status, self.status)

    @property
    def channel_label(self):
        return CHANNEL_LABELS.get(self.channel, "—")

    @property
    def is_editable(self):
        return self.status in CampaignStatus.EDITABLE

    @property
    def sent_or_scheduled_at(self):
        return self.queued_at or self.scheduled_at or self.created_at


class CampaignRecipient(db.Model):
    __tablename__ = "campaign_recipients"

    STATUS_PENDING = "pending"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"  # e.g. opted out between scheduling and sending

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    address = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id", ondelete="SET NULL"))
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    processed_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint("campaign_id", "client_id", name="uq_campaign_recipients_campaign_client"),
        db.Index("ix_campaign_recipients_campaign_status", "campaign_id", "status"),
    )

    campaign = db.relationship("Campaign", back_populates="recipients")
    client = db.relationship("Client")
    message = db.relationship("Message", foreign_keys=[message_id])


class MessageTemplate(db.Model):
    __tablename__ = "message_templates"

    STATUS_ACTIVE = "active"
    STATUS_ARCHIVED = "archived"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    channel = db.Column(db.String(20), nullable=False)
    subject = db.Column(db.String(255))
    content = db.Column(db.Text, nullable=False)
    is_html = db.Column(db.Boolean, nullable=False, default=False)
    # Name of the pre-approved template at the WhatsApp provider, if any.
    provider_template_name = db.Column(db.String(120))
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.UniqueConstraint("name", "channel", name="uq_message_templates_name_channel"),
        db.CheckConstraint("channel IN ('sms', 'whatsapp', 'email')", name="channel_valid"),
    )

    created_by = db.relationship("User")

    @property
    def channel_label(self):
        return CHANNEL_LABELS.get(self.channel, self.channel)
