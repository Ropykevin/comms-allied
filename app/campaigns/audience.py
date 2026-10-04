"""Audience segmentation: Category → optional filters → recipient count per channel.

Within one filter type values are OR-ed (any of the chosen categories);
different filter types are AND-ed (category AND tag AND status AND location).
Archived clients are never included, and each channel only counts clients
with an address on that channel who have not opted out of it.
"""
from sqlalchemy import and_, exists, func, select

from app.extensions import db
from app.models import CHANNELS, Client, ClientStatus, ContactChannel, client_categories, client_tags


def _int_list(values):
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def normalize_audience(data):
    data = data or {}
    statuses = [s for s in (data.get("statuses") or []) if s in ClientStatus.ALL and s != ClientStatus.ARCHIVED]
    return {
        "category_ids": _int_list(data.get("category_ids")),
        "tag_ids": _int_list(data.get("tag_ids")),
        "statuses": sorted(set(statuses)),
        "location": (data.get("location") or "").strip()[:120],
    }


def audience_from_form(form):
    return normalize_audience({
        "category_ids": form.getlist("category_ids"),
        "tag_ids": form.getlist("tag_ids"),
        "statuses": form.getlist("statuses"),
        "location": form.get("location"),
    })


def _conditions(audience):
    conds = [Client.status != ClientStatus.ARCHIVED]
    if audience["statuses"]:
        conds.append(Client.status.in_(audience["statuses"]))
    if audience["category_ids"]:
        conds.append(exists().where(
            client_categories.c.client_id == Client.id,
            client_categories.c.category_id.in_(audience["category_ids"]),
        ))
    if audience["tag_ids"]:
        conds.append(exists().where(client_tags.c.client_id == Client.id, client_tags.c.tag_id.in_(audience["tag_ids"])))
    if audience["location"]:
        conds.append(Client.location.ilike(f"%{audience['location']}%"))
    return conds


def audience_client_ids(audience):
    return select(Client.id).where(and_(*_conditions(audience)))


def _reachable(channel):
    return and_(
        ContactChannel.client_id == Client.id,
        ContactChannel.channel == channel,
        ContactChannel.status == ContactChannel.STATUS_ALLOWED,
        ContactChannel.address.isnot(None),
        ContactChannel.address != "",
    )


def estimate(audience):
    """Recipient counts for every channel, with the reasons clients are excluded."""
    audience = normalize_audience(audience)
    conds = _conditions(audience)
    total = db.session.execute(select(func.count(Client.id)).where(*conds)).scalar()

    channels = {}
    for channel in CHANNELS:
        available = db.session.execute(
            select(func.count(Client.id)).where(*conds, exists().where(_reachable(channel)))
        ).scalar()
        opted_out = db.session.execute(
            select(func.count(Client.id)).where(*conds, exists().where(
                ContactChannel.client_id == Client.id,
                ContactChannel.channel == channel,
                ContactChannel.status == ContactChannel.STATUS_OPTED_OUT,
            ))
        ).scalar()
        channels[channel] = {
            "available": available,
            "excluded": total - available,
            "opted_out": opted_out,
            "missing_contact": max(total - available - opted_out, 0),
        }
    return {"total": total, "channels": channels}


def eligible_recipients(audience, channel):
    """[(client_id, address)] for everyone in the audience reachable on this channel."""
    audience = normalize_audience(audience)
    stmt = (
        select(Client.id, ContactChannel.address)
        .join(ContactChannel, _reachable(channel))
        .where(*_conditions(audience))
        .order_by(Client.id)
    )
    return db.session.execute(stmt).all()


def sample_client(audience, channel=None):
    audience = normalize_audience(audience)
    q = Client.query.filter(*_conditions(audience))
    if channel in CHANNELS:
        q = q.filter(exists().where(_reachable(channel)))
    return q.order_by(Client.full_name).first()
