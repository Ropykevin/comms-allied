from app.extensions import db
from app.utils import utcnow


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(50), index=True)
    entity_id = db.Column(db.Integer)
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    user = db.relationship("User")


class WebhookEvent(db.Model):
    """Every webhook call received, keyed so that provider retries are processed once."""

    __tablename__ = "webhook_events"

    STATUS_RECEIVED = "received"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"
    STATUS_IGNORED = "ignored"

    id = db.Column(db.Integer, primary_key=True)
    channel = db.Column(db.String(20), nullable=False)
    provider = db.Column(db.String(40), nullable=False)
    event_key = db.Column(db.String(255), nullable=False)
    event_type = db.Column(db.String(50))
    payload = db.Column(db.JSON)
    status = db.Column(db.String(20), nullable=False, default=STATUS_RECEIVED)
    error_message = db.Column(db.Text)
    received_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    processed_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint("channel", "event_key", name="uq_webhook_events_channel_event_key"),
    )
