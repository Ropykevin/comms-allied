from sqlalchemy import exists, or_, select
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    Category,
    Client,
    ClientStatus,
    ContactChannel,
    Tag,
    client_categories,
    client_tags,
)
from app.utils import normalize_email, normalize_phone

PER_PAGE = 25


def save_client(client, data, categories=None, tags=None):
    """Apply form/import data to a client and keep its contact channels in sync.

    data keys: full_name, phone, whatsapp, email, company, location, status, notes,
               sms_allowed, whatsapp_allowed, email_allowed
    """
    client.full_name = (data.get("full_name") or "").strip()
    client.phone = normalize_phone(data.get("phone"))
    client.email = normalize_email(data.get("email"))
    client.company = (data.get("company") or "").strip() or None
    client.location = (data.get("location") or "").strip() or None
    client.status = data.get("status") or client.status or ClientStatus.ACTIVE
    client.notes = (data.get("notes") or "").strip() or None
    if categories is not None:
        client.categories = list(categories)
    if tags is not None:
        client.tags = list(tags)
    if client.id is None:
        db.session.add(client)

    whatsapp = normalize_phone(data.get("whatsapp")) if data.get("whatsapp") else client.phone
    addresses = {CHANNEL_SMS: client.phone, CHANNEL_WHATSAPP: whatsapp, CHANNEL_EMAIL: client.email}
    for channel, address in addresses.items():
        _sync_channel(client, channel, address, data.get(f"{channel}_allowed", True))
    db.session.flush()
    return client


def _sync_channel(client, channel, address, allowed):
    cc = client.channel(channel)
    if cc is None:
        if not address:
            return
        cc = ContactChannel(channel=channel, status=ContactChannel.STATUS_ALLOWED)
        client.contact_channels.append(cc)
    cc.address = address
    if allowed and not cc.is_allowed:
        cc.opt_in()
    elif not allowed and cc.is_allowed:
        cc.opt_out("staff")


def find_duplicate(phone=None, email=None, exclude_id=None):
    phone = normalize_phone(phone)
    email = normalize_email(email)
    conditions = []
    if phone:
        conditions.append(Client.phone == phone)
    if email:
        conditions.append(Client.email == email)
    if not conditions:
        return None
    q = Client.query.filter(or_(*conditions))
    if exclude_id:
        q = q.filter(Client.id != exclude_id)
    return q.first()


def get_or_create_tags(names):
    tags = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        tag = Tag.query.filter(db.func.lower(Tag.name) == name.lower()).first()
        if tag is None:
            tag = Tag(name=name[:60])
            db.session.add(tag)
            db.session.flush()
        if tag not in tags:
            tags.append(tag)
    return tags


def parse_filters(args):
    def ints(key):
        out = []
        for v in args.getlist(key):
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out

    return {
        "q": (args.get("q") or "").strip(),
        "status": args.get("status") or "",
        "category": ints("category"),
        "tag": ints("tag"),
        "location": (args.get("location") or "").strip(),
        "channel": args.get("channel") or "",
        "sort": args.get("sort") or "newest",
    }


def filtered_clients(filters):
    q = Client.query.options(selectinload(Client.categories), selectinload(Client.contact_channels))

    status = filters.get("status")
    if status == "all":
        pass
    elif status in ClientStatus.ALL:
        q = q.filter(Client.status == status)
    else:
        q = q.filter(Client.status != ClientStatus.ARCHIVED)

    term = filters.get("q")
    if term:
        like = f"%{term}%"
        conditions = [Client.full_name.ilike(like), Client.email.ilike(like), Client.company.ilike(like),
                      Client.phone.ilike(like)]
        phone = normalize_phone(term)
        if phone:
            conditions.append(Client.phone == phone)
        q = q.filter(or_(*conditions))

    if filters.get("category"):
        q = q.filter(exists().where(
            client_categories.c.client_id == Client.id, client_categories.c.category_id.in_(filters["category"])
        ))
    if filters.get("tag"):
        q = q.filter(exists().where(client_tags.c.client_id == Client.id, client_tags.c.tag_id.in_(filters["tag"])))
    if filters.get("location"):
        q = q.filter(Client.location.ilike(f"%{filters['location']}%"))
    if filters.get("channel") in (CHANNEL_SMS, CHANNEL_WHATSAPP, CHANNEL_EMAIL):
        q = q.filter(exists().where(
            ContactChannel.client_id == Client.id, ContactChannel.channel == filters["channel"],
            ContactChannel.status == ContactChannel.STATUS_ALLOWED, ContactChannel.address.isnot(None),
        ))

    order = {
        "newest": Client.created_at.desc(),
        "oldest": Client.created_at.asc(),
        "name": Client.full_name.asc(),
        "last_contact": Client.last_contacted_at.desc().nullslast(),
    }.get(filters.get("sort"), Client.created_at.desc())
    return q.order_by(order, Client.id.desc())


def bulk_apply(client_ids, action, target_id=None):
    """Bulk categorize/tag/archive. Returns number of clients affected."""
    clients = Client.query.filter(Client.id.in_(client_ids)).all()
    if action in ("add_category", "remove_category"):
        category = db.session.get(Category, target_id)
        if category is None:
            return 0
        for c in clients:
            if action == "add_category" and category not in c.categories:
                c.categories.append(category)
            elif action == "remove_category" and category in c.categories:
                c.categories.remove(category)
    elif action in ("add_tag", "remove_tag"):
        tag = db.session.get(Tag, target_id)
        if tag is None:
            return 0
        for c in clients:
            if action == "add_tag" and tag not in c.tags:
                c.tags.append(tag)
            elif action == "remove_tag" and tag in c.tags:
                c.tags.remove(tag)
    elif action == "archive":
        for c in clients:
            c.status = ClientStatus.ARCHIVED
    else:
        return 0
    return len(clients)


def category_counts():
    """{category_id: number of non-archived clients}."""
    rows = db.session.execute(
        select(client_categories.c.category_id, db.func.count())
        .join(Client, Client.id == client_categories.c.client_id)
        .where(Client.status != ClientStatus.ARCHIVED)
        .group_by(client_categories.c.category_id)
    ).all()
    return dict(rows)


def tag_counts():
    rows = db.session.execute(
        select(client_tags.c.tag_id, db.func.count())
        .join(Client, Client.id == client_tags.c.client_id)
        .where(Client.status != ClientStatus.ARCHIVED)
        .group_by(client_tags.c.tag_id)
    ).all()
    return dict(rows)
