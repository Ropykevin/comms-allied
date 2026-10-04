from app.extensions import db
from app.utils import utcnow

client_tags = db.Table(
    "client_tags",
    db.Column("client_id", db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    db.Column("created_at", db.DateTime, nullable=False, default=utcnow),
    db.Index("ix_client_tags_tag_id", "tag_id"),
)


class Tag(db.Model):
    __tablename__ = "tags"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    clients = db.relationship("Client", secondary=client_tags, back_populates="tags", lazy="dynamic")

    def __repr__(self):
        return f"<Tag {self.name}>"
