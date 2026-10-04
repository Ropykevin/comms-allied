from app.extensions import db
from app.utils import utcnow

client_categories = db.Table(
    "client_categories",
    db.Column("client_id", db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True),
    db.Column("category_id", db.Integer, db.ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
    db.Column("created_at", db.DateTime, nullable=False, default=utcnow),
    db.Index("ix_client_categories_category_id", "category_id"),
)


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(90), unique=True, nullable=False)
    description = db.Column(db.String(255))
    color = db.Column(db.String(20), nullable=False, default="indigo")
    is_featured = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    clients = db.relationship("Client", secondary=client_categories, back_populates="categories", lazy="dynamic")

    COLORS = ["indigo", "emerald", "amber", "sky", "rose", "violet", "teal", "orange", "slate", "lime"]

    def __repr__(self):
        return f"<Category {self.name}>"
