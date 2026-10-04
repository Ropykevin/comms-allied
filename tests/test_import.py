import io
import re

from app.models import Client
from tests.helpers import category, make_client, tag

CSV = (
    "Name,Phone,Email,Company,Location,Categories,Tags\n"
    "John Kamau,+254712345678,john@example.com,ABC Ltd,Nairobi,Safari Clients,Interested\n"
    "Mary Wanjiku,0722222222,mary@example.com,XYZ Ltd,Mombasa,VIP Clients;Corporate Clients,\n"
    "Bad Phone,12345,,,,,\n"
    ",+254733333333,noname@example.com,,,,\n"
    "Dup In File,+254722222222,,,,,\n"
    "Existing Person,+254700000001,,,,,\n"
)


def _upload(c, content=CSV):
    resp = c.post("/clients/import", data={"file": (io.BytesIO(content.encode()), "clients.csv")},
                  content_type="multipart/form-data")
    assert resp.status_code == 302, resp.data
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def test_import_preview_then_confirm(login_as, db):
    make_client("Existing Person", "+254700000001", None)
    c = login_as("admin")
    token = _upload(c)
    assert re.fullmatch(r"[a-f0-9]{32}", token)

    preview = c.get(f"/clients/import/{token}")
    assert preview.status_code == 200
    html = preview.get_data(as_text=True)
    assert "Ready to import" in html
    assert Client.query.count() == 1  # nothing written during preview

    from app.clients.importer import analyze
    summary = analyze(token)
    assert summary["total"] == 6
    assert summary["valid"] == 2
    assert summary["duplicates"] == 2  # existing client + repeated phone in file
    assert summary["invalid"] == 2

    errors = c.get(f"/clients/import/{token}/errors.csv").get_data(as_text=True)
    assert "Invalid phone" in errors and "Name is missing" in errors

    resp = c.post(f"/clients/import/{token}", data={"category": [category("New Clients").id], "tag": [tag("2026").id]})
    assert resp.status_code == 200
    assert b"Import complete" in resp.data
    assert Client.query.count() == 3

    mary = Client.query.filter_by(email="mary@example.com").one()
    assert mary.phone == "+254722222222"
    assert {c.name for c in mary.categories} == {"VIP Clients", "Corporate Clients", "New Clients"}
    assert [t.name for t in mary.tags] == ["2026"]
    john = Client.query.filter_by(email="john@example.com").one()
    assert {t.name for t in john.tags} == {"Interested", "2026"}

    # The error report remains available after import and still lists only the rejected rows.
    errors = c.get(f"/clients/import/{token}/errors.csv").get_data(as_text=True)
    assert "John Kamau" not in errors and "Bad Phone" in errors


def test_import_requires_name_column(login_as):
    c = login_as("admin")
    token = _upload(c, "Phone,Email\n+254712345678,a@example.com\n")
    resp = c.get(f"/clients/import/{token}")
    assert b"Name" in resp.data and b"required" in resp.data


def test_import_rejects_bad_token(login_as):
    c = login_as("admin")
    resp = c.get("/clients/import/..%2F..%2Fetc")
    assert resp.status_code in (302, 404)


def test_import_permission(login_as):
    c = login_as("marketing")
    assert c.get("/clients/import").status_code == 403
