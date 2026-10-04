from app.extensions import db
from app.models.category import client_categories
from app.models.tag import client_tags
from app.utils import utcnow

CHANNEL_SMS = "sms"
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_EMAIL = "email"
CHANNELS = (CHANNEL_SMS, CHANNEL_WHATSAPP, CHANNEL_EMAIL)
CHANNEL_LABELS = {CHANNEL_SMS: "SMS", CHANNEL_WHATSAPP: "WhatsApp", CHANNEL_EMAIL: "Email"}


class ClientStatus:
    ACTIVE = "active"
    INACTIVE = "inactive"
    PROSPECT = "prospect"
    ARCHIVED = "archived"
    ALL = (ACTIVE, INACTIVE, PROSPECT, ARCHIVED)
    LABELS = {ACTIVE: "Active", INACTIVE: "Inactive", PROSPECT: "Prospect", ARCHIVED: "Archived"}


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False, index=True)
    phone = db.Column(db.String(32), index=True)
    email = db.Column(db.String(255), index=True)
    company = db.Column(db.String(150))
    location = db.Column(db.String(120), index=True)
    status = db.Column(db.String(20), nullable=False, default=ClientStatus.ACTIVE, index=True)
    notes = db.Column(db.Text)
    last_contacted_at = db.Column(db.DateTime)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('active', 'inactive', 'prospect', 'archived')", name="status_valid"
        ),
    )

    categories = db.relationship(
        "Category", secondary=client_categories, back_populates="clients", order_by="Category.name"
    )
    tags = db.relationship("Tag", secondary=client_tags, back_populates="clients", order_by="Tag.name")
    contact_channels = db.relationship(
        "ContactChannel", back_populates="client", cascade="all, delete-orphan", order_by="ContactChannel.channel"
    )
    conversations = db.relationship("Conversation", back_populates="client", cascade="all, delete-orphan")
    created_by = db.relationship("User")

    @property
    def first_name(self):
        parts = (self.full_name or "").split()
        return parts[0] if parts else ""

    @property
    def last_name(self):
        parts = (self.full_name or "").split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    @property
    def initials(self):
        parts = (self.full_name or "").split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def status_label(self):
        return ClientStatus.LABELS.get(self.status, self.status.title())

    def channel(self, name):
        for cc in self.contact_channels:
            if cc.channel == name:
                return cc
        return None

    def __repr__(self):
        return f"<Client {self.full_name}>"


class ContactChannel(db.Model):
    """A client's address on one channel plus their consent for that channel."""

    __tablename__ = "contact_channels"

    STATUS_ALLOWED = "allowed"
    STATUS_OPTED_OUT = "opted_out"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    channel = db.Column(db.String(20), nullable=False)
    address = db.Column(db.String(255))
    status = db.Column(db.String(20), nullable=False, default=STATUS_ALLOWED)
    opted_out_at = db.Column(db.DateTime)
    opt_out_source = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.UniqueConstraint("client_id", "channel", name="uq_contact_channels_client_channel"),
        db.Index("ix_contact_channels_channel_address", "channel", "address"),
        db.CheckConstraint("channel IN ('sms', 'whatsapp', 'email')", name="channel_valid"),
        db.CheckConstraint("status IN ('allowed', 'opted_out')", name="status_valid"),
    )

    client = db.relationship("Client", back_populates="contact_channels")

    @property
    def is_allowed(self):
        return self.status == self.STATUS_ALLOWED

    @property
    def is_reachable(self):
        return self.is_allowed and bool(self.address)

    @property
    def label(self):
        return CHANNEL_LABELS.get(self.channel, self.channel)

    def opt_out(self, source="manual"):
        self.status = self.STATUS_OPTED_OUT
        self.opted_out_at = utcnow()
        self.opt_out_source = source

    def opt_in(self):
        self.status = self.STATUS_ALLOWED
        self.opted_out_at = None
        self.opt_out_source = None
