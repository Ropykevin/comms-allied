from datetime import timedelta

from flask import Blueprint, render_template
from flask_login import login_required
from sqlalchemy import func

from app.analytics import channel_stats, message_stats
from app.extensions import db
from app.models import Campaign, Category, Client, ClientStatus, Conversation, client_categories
from app.utils import utcnow

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

NEW_CLIENT_DAYS = 30


def client_stats():
    counts = dict(db.session.query(Client.status, func.count(Client.id)).group_by(Client.status).all())
    total = sum(v for k, v in counts.items() if k != ClientStatus.ARCHIVED)
    new = (
        db.session.query(func.count(Client.id))
        .filter(Client.created_at >= utcnow() - timedelta(days=NEW_CLIENT_DAYS), Client.status != ClientStatus.ARCHIVED)
        .scalar()
    )
    return {
        "total": total,
        "active": counts.get(ClientStatus.ACTIVE, 0),
        "inactive": counts.get(ClientStatus.INACTIVE, 0),
        "prospect": counts.get(ClientStatus.PROSPECT, 0),
        "new": new,
    }


def featured_category_stats():
    rows = (
        db.session.query(Category, func.count(Client.id))
        .outerjoin(client_categories, client_categories.c.category_id == Category.id)
        .outerjoin(Client, (Client.id == client_categories.c.client_id) & (Client.status != ClientStatus.ARCHIVED))
        .filter(Category.is_featured.is_(True))
        .group_by(Category.id)
        .order_by(Category.name)
        .all()
    )
    return rows


@bp.route("/")
@login_required
def index():
    recent_campaigns = Campaign.query.order_by(Campaign.created_at.desc()).limit(6).all()
    category_names = _audience_names(recent_campaigns)
    recent_conversations = (
        Conversation.query.filter(Conversation.last_message_at.isnot(None))
        .order_by(Conversation.last_message_at.desc())
        .limit(6)
        .all()
    )
    return render_template(
        "dashboard/index.html",
        clients=client_stats(),
        featured_categories=featured_category_stats(),
        campaign_count=db.session.query(func.count(Campaign.id)).scalar(),
        messages=message_stats(),
        channels=channel_stats(),
        recent_campaigns=recent_campaigns,
        category_names=category_names,
        recent_conversations=recent_conversations,
    )


def _audience_names(campaigns):
    ids = {cid for c in campaigns for cid in (c.audience or {}).get("category_ids", [])}
    if not ids:
        return {}
    return dict(db.session.query(Category.id, Category.name).filter(Category.id.in_(ids)).all())
