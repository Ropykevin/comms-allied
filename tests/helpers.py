from app.clients.services import save_client
from app.extensions import db
from app.models import Category, Client, Tag


def make_client(name="John Kamau", phone="+254712345678", email="john@example.com", categories=(), tags=(),
                status="active", location="Nairobi", company="ABC Ltd", whatsapp=None, **prefs):
    client = Client()
    data = {
        "full_name": name, "phone": phone, "email": email, "company": company, "location": location,
        "status": status, "whatsapp": whatsapp,
        "sms_allowed": prefs.get("sms_allowed", True),
        "whatsapp_allowed": prefs.get("whatsapp_allowed", True),
        "email_allowed": prefs.get("email_allowed", True),
    }
    cats = [Category.query.filter_by(name=c).one() for c in categories]
    tag_objs = [Tag.query.filter_by(name=t).one() for t in tags]
    save_client(client, data, cats, tag_objs)
    db.session.commit()
    return client


def category(name):
    return Category.query.filter_by(name=name).one()


def tag(name):
    return Tag.query.filter_by(name=name).one()
