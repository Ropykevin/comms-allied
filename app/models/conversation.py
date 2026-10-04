from app.extensions import db
from app.models.client import CHANNEL_LABELS
from app.utils import utcnow


class ConversationStatus:
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ALL = (OPEN, PENDING, RESOLVED, CLOSED)
    LABELS = {OPEN: "Open", PENDING: "Pending", RESOLVED: "Resolved", CLOSED: "Closed"}


class Conversation(db.Model):
    """One thread per client per channel — SMS, WhatsApp and Email are never mixed."""

    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    channel = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=ConversationStatus.OPEN, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    subject = db.Column(db.String(255))
    last_message_at = db.Column(db.DateTime, index=True)
    last_message_preview = db.Column(db.String(255))
    last_message_direction = db.Column(db.String(10))
    unread_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.UniqueConstraint("client_id", "channel", name="uq_conversations_client_channel"),
        db.CheckConstraint("channel IN ('sms', 'whatsapp', 'email')", name="channel_valid"),
        db.CheckConstraint("status IN ('open', 'pending', 'resolved', 'closed')", name="status_valid"),
    )

    client = db.relationship("Client", back_populates="conversations")
    assigned_to = db.relationship("User")
    messages = db.relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan",
        order_by="Message.created_at", lazy="dynamic",
    )
    notes = db.relationship(
        "ConversationNote", back_populates="conversation", cascade="all, delete-orphan",
        order_by="ConversationNote.created_at",
    )

    @property
    def channel_label(self):
        return CHANNEL_LABELS.get(self.channel, self.channel)

    @property
    def status_label(self):
        return ConversationStatus.LABELS.get(self.status, self.status)


class ConversationNote(db.Model):
    """Internal note visible to staff only; never sent to the client."""

    __tablename__ = "conversation_notes"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    conversation = db.relationship("Conversation", back_populates="notes")
    user = db.relationship("User")
