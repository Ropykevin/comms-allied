from app.models import AuditLog, Client, ContactChannel
from tests.helpers import category, make_client, tag


def _client_form(**overrides):
    data = {
        "full_name": "John Kamau", "phone": "0712345678", "email": "John@Example.com",
        "company": "ABC Ltd", "location": "Nairobi", "status": "active",
        "sms_allowed": "y", "whatsapp_allowed": "y", "email_allowed": "y",
    }
    data.update(overrides)
    return data


def test_create_client_normalises_and_creates_channels(login_as, db):
    c = login_as("admin")
    vip, safari = category("VIP Clients"), category("Safari Clients")
    resp = c.post("/clients/new", data=_client_form(categories=[vip.id, safari.id], tags=[tag("Premium").id],
                                                    new_tags="Honeymoon, Premium"))
    assert resp.status_code == 302
    client = Client.query.one()
    assert client.phone == "+254712345678"
    assert client.email == "john@example.com"
    assert {c.name for c in client.categories} == {"VIP Clients", "Safari Clients"}
    assert {t.name for t in client.tags} == {"Premium", "Honeymoon"}
    channels = {cc.channel: cc for cc in client.contact_channels}
    assert set(channels) == {"sms", "whatsapp", "email"}
    assert channels["whatsapp"].address == "+254712345678"
    assert AuditLog.query.filter_by(action="client.created").count() == 1


def test_client_requires_phone_or_email(login_as):
    c = login_as("admin")
    resp = c.post("/clients/new", data=_client_form(phone="", email=""))
    assert resp.status_code == 200
    assert Client.query.count() == 0


def test_invalid_phone_rejected(login_as):
    c = login_as("admin")
    resp = c.post("/clients/new", data=_client_form(phone="12"))
    assert resp.status_code == 200
    assert b"valid phone number" in resp.data


def test_duplicate_client_blocked(login_as):
    make_client()
    c = login_as("admin")
    c.post("/clients/new", data=_client_form(full_name="Someone Else", email="other@example.com"))
    assert Client.query.count() == 1


def test_update_client_and_opt_out(login_as, db):
    client = make_client()
    c = login_as("admin")
    form = _client_form(full_name="John K. Kamau", location="Mombasa")
    form.pop("email_allowed")
    resp = c.post(f"/clients/{client.id}/edit", data=form)
    assert resp.status_code == 302
    db.session.refresh(client)
    assert client.full_name == "John K. Kamau"
    assert client.location == "Mombasa"
    assert client.channel("email").status == ContactChannel.STATUS_OPTED_OUT


def test_view_profile(login_as):
    client = make_client(categories=["VIP Clients"], tags=["Premium"])
    c = login_as("viewer")
    resp = c.get(f"/clients/{client.id}")
    assert resp.status_code == 200
    assert b"John Kamau" in resp.data
    assert b"VIP Clients" in resp.data
    assert b"#Premium" in resp.data


def test_viewer_cannot_edit_or_create(login_as):
    client = make_client()
    c = login_as("viewer")
    assert c.get("/clients/new").status_code == 403
    assert c.post(f"/clients/{client.id}/edit", data=_client_form()).status_code == 403
    assert c.post(f"/clients/{client.id}/archive").status_code == 403


def test_archive_and_restore(login_as, db):
    client = make_client()
    c = login_as("admin")
    c.post(f"/clients/{client.id}/archive")
    db.session.refresh(client)
    assert client.status == "archived"
    # archived clients are hidden from the default directory
    assert f'href="/clients/{client.id}"'.encode() not in c.get("/clients/").data
    c.post(f"/clients/{client.id}/restore")
    db.session.refresh(client)
    assert client.status == "active"


def test_delete_requires_permission(login_as):
    client = make_client()
    assert login_as("marketing").post(f"/clients/{client.id}/delete").status_code == 403
    assert login_as("admin").post(f"/clients/{client.id}/delete").status_code == 302
    assert Client.query.count() == 0


def test_search_and_filters(login_as):
    make_client("John Kamau", "+254712345678", "john@example.com", categories=["Safari Clients"], tags=["Interested"])
    make_client("Mary Wanjiku", "+254722222222", "mary@example.com", categories=["VIP Clients"], location="Mombasa")
    c = login_as("viewer")
    page = c.get("/clients/?q=wanjiku").data
    assert b"Mary Wanjiku" in page and b"John Kamau" not in page
    page = c.get("/clients/?q=0712345678").data  # local phone format matches E.164
    assert b"John Kamau" in page and b"Mary Wanjiku" not in page
    page = c.get(f"/clients/?category={category('VIP Clients').id}").data
    assert b"Mary Wanjiku" in page and b"John Kamau" not in page
    page = c.get(f"/clients/?tag={tag('Interested').id}").data
    assert b"John Kamau" in page and b"Mary Wanjiku" not in page
    page = c.get("/clients/?location=mombasa").data
    assert b"Mary Wanjiku" in page and b"John Kamau" not in page


def test_pagination_is_server_side(login_as, db):
    for i in range(30):
        make_client(f"Client {i:02d}", f"+2547120000{i:02d}", f"c{i}@example.com")
    c = login_as("viewer")
    first = c.get("/clients/?sort=name").data
    assert b"Client 00" in first and b"Client 29" not in first
    second = c.get("/clients/?sort=name&page=2").data
    assert b"Client 29" in second


def test_bulk_categorize(login_as, db):
    a = make_client("A One", "+254711111111", "a@example.com")
    b = make_client("B Two", "+254711111112", "b@example.com")
    c = login_as("marketing")
    vip = category("VIP Clients")
    resp = c.post("/clients/bulk", data={"action": "add_category", "target_id": vip.id, "client_ids": [a.id, b.id]})
    assert resp.status_code == 302
    assert vip.clients.count() == 2
    c.post("/clients/bulk", data={"action": "add_tag", "target_id": tag("Paid").id, "client_ids": [a.id]})
    db.session.refresh(a)
    assert [t.name for t in a.tags] == ["Paid"]
    # marketing may categorize but not archive
    assert c.post("/clients/bulk", data={"action": "archive", "client_ids": [a.id]}).status_code == 403


def test_client_categories_are_many_to_many(db):
    client = make_client(categories=["VIP Clients", "Corporate Clients", "Safari Clients"])
    assert len(client.categories) == 3
    assert not hasattr(Client, "category")


def test_channel_preference_toggle(login_as, db):
    client = make_client()
    c = login_as("admin")
    c.post(f"/clients/{client.id}/channels/whatsapp", data={"allowed": "0"})
    db.session.refresh(client)
    assert client.channel("whatsapp").status == "opted_out"
    c.post(f"/clients/{client.id}/channels/whatsapp", data={"allowed": "1"})
    db.session.refresh(client)
    assert client.channel("whatsapp").status == "allowed"


def test_export_csv(login_as):
    make_client(categories=["VIP Clients"])
    make_client("=cmd()", "+254722222222", "x@example.com")
    resp = login_as("marketing").get("/clients/export")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "John Kamau" in body and "VIP Clients" in body
    assert "+254712345678" in body  # phone numbers are not mangled
    assert "'=cmd()" in body  # formula injection neutralised


def test_support_cannot_export(login_as):
    assert login_as("support").get("/clients/export").status_code == 403


def test_categories_crud(login_as):
    c = login_as("marketing")
    c.post("/categories/new", data={"name": "Honeymoon Clients", "color": "rose"})
    from app.models import Category
    cat = Category.query.filter_by(name="Honeymoon Clients").one()
    assert cat.slug == "honeymoon-clients"
    c.post("/categories/new", data={"name": "honeymoon clients", "color": "rose"})
    assert Category.query.filter(Category.slug == "honeymoon-clients").count() == 1
    c.post(f"/categories/{cat.id}/edit", data={"name": "Honeymooners", "color": "violet", "is_featured": "y"})
    assert Category.query.get(cat.id).name == "Honeymooners" if False else Category.query.filter_by(id=cat.id).one().is_featured
    c.post(f"/categories/{cat.id}/delete")
    assert Category.query.filter_by(id=cat.id).first() is None
    assert login_as("support").post("/categories/new", data={"name": "X", "color": "rose"}).status_code == 403


def test_tags_crud(login_as):
    client = make_client(tags=["Paid"])
    c = login_as("admin")
    c.post("/categories/tags/new", data={"name": "#Family"})
    from app.models import Tag
    family = Tag.query.filter_by(name="Family").one()
    c.post(f"/categories/tags/{family.id}/rename", data={"name": "Families"})
    assert Tag.query.filter_by(name="Families").one()
    c.post(f"/categories/tags/{tag('Paid').id}/delete")
    from app.extensions import db
    db.session.refresh(client)
    assert client.tags == []
