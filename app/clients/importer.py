"""CSV client import: Upload → Validate → Preview → Confirm → Import."""
import csv
import io
import os
import re
import uuid

from email_validator import EmailNotValidError, validate_email
from flask import current_app

from app.clients.services import get_or_create_tags, save_client
from app.extensions import db
from app.models import Category, Client, ClientStatus
from app.utils import normalize_email, normalize_phone

MAX_ROWS = 20000
TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")

HEADER_ALIASES = {
    "full_name": {"name", "full name", "full_name", "client", "client name"},
    "phone": {"phone", "phone number", "mobile", "telephone", "sms"},
    "whatsapp": {"whatsapp", "whatsapp number"},
    "email": {"email", "email address", "e-mail"},
    "company": {"company", "organisation", "organization", "business"},
    "location": {"location", "city", "town"},
    "status": {"status"},
    "categories": {"categories", "category"},
    "tags": {"tags", "tag"},
    "notes": {"notes", "note", "comments"},
}
SAMPLE_CSV = (
    "Name,Phone,Email,Company,Location\n"
    "John Kamau,+254712345678,john@example.com,ABC Ltd,Nairobi\n"
    "Mary Wanjiku,+254722222222,mary@example.com,XYZ Ltd,Mombasa\n"
)


def _import_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "imports")
    os.makedirs(path, exist_ok=True)
    return path


def _path_for(token):
    if not TOKEN_RE.match(token or ""):
        return None
    path = os.path.join(_import_dir(), f"{token}.csv")
    return path if os.path.exists(path) else None


def store_upload(file_storage):
    token = uuid.uuid4().hex
    file_storage.save(os.path.join(_import_dir(), f"{token}.csv"))
    return token


def discard(token):
    path = _path_for(token)
    if path:
        os.remove(path)


def _read_text(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _map_headers(headers):
    mapping = {}
    for idx, header in enumerate(headers):
        key = (header or "").strip().lower()
        for field, aliases in HEADER_ALIASES.items():
            if key in aliases and field not in mapping:
                mapping[field] = idx
    return mapping


def analyze(token):
    """Validate every row without writing anything. Returns a summary dict or None for a bad token."""
    path = _path_for(token)
    if path is None:
        return None
    reader = csv.reader(io.StringIO(_read_text(path)))
    try:
        headers = next(reader)
    except StopIteration:
        return {"error": "The file is empty.", "rows": [], "total": 0}
    mapping = _map_headers(headers)
    if "full_name" not in mapping:
        return {"error": "A 'Name' column is required.", "rows": [], "total": 0}
    if "phone" not in mapping and "email" not in mapping:
        return {"error": "A 'Phone' or 'Email' column is required.", "rows": [], "total": 0}

    existing_phones, existing_emails = _existing_contacts()
    seen_phones, seen_emails = set(), set()
    rows = []
    for line_no, raw in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in raw):
            continue
        if len(rows) >= MAX_ROWS:
            return {"error": f"Files are limited to {MAX_ROWS:,} rows.", "rows": [], "total": 0}
        get = lambda f: (raw[mapping[f]].strip() if f in mapping and mapping[f] < len(raw) else "")  # noqa: E731
        row = {f: get(f) for f in HEADER_ALIASES}
        row["line"] = line_no
        errors = []
        if not row["full_name"]:
            errors.append("Name is missing")
        phone = normalize_phone(row["phone"]) if row["phone"] else None
        if row["phone"] and not phone:
            errors.append(f"Invalid phone '{row['phone']}'")
        if row["whatsapp"] and not normalize_phone(row["whatsapp"]):
            errors.append(f"Invalid WhatsApp number '{row['whatsapp']}'")
        email = None
        if row["email"]:
            try:
                email = normalize_email(validate_email(row["email"], check_deliverability=False).normalized)
            except EmailNotValidError:
                errors.append(f"Invalid email '{row['email']}'")
        if not row["phone"] and not row["email"]:
            errors.append("Phone or email is required")
        status = (row["status"] or ClientStatus.ACTIVE).lower()
        if status not in ClientStatus.ALL:
            errors.append(f"Unknown status '{row['status']}'")
        row.update(phone=phone or row["phone"], email=email or row["email"], status=status)

        if errors:
            row["result"], row["reason"] = "invalid", "; ".join(errors)
        elif (phone and phone in existing_phones) or (email and email in existing_emails):
            row["result"], row["reason"] = "duplicate", "A client with this phone or email already exists"
        elif (phone and phone in seen_phones) or (email and email in seen_emails):
            row["result"], row["reason"] = "duplicate", "Repeated earlier in this file"
        else:
            row["result"], row["reason"] = "valid", ""
            if phone:
                seen_phones.add(phone)
            if email:
                seen_emails.add(email)
        rows.append(row)

    return {
        "error": None,
        "rows": rows,
        "total": len(rows),
        "valid": sum(r["result"] == "valid" for r in rows),
        "duplicates": sum(r["result"] == "duplicate" for r in rows),
        "invalid": sum(r["result"] == "invalid" for r in rows),
        "columns": sorted(mapping),
    }


def _existing_contacts():
    phones, emails = set(), set()
    for phone, email in db.session.query(Client.phone, Client.email).yield_per(5000):
        if phone:
            phones.add(phone)
        if email:
            emails.add(email)
    return phones, emails


def run_import(token, category_ids=None, tag_ids=None, created_by=None):
    """Create clients for every valid row. Returns the analysis summary plus 'imported'."""
    summary = analyze(token)
    if summary is None or summary.get("error"):
        return summary
    shared_categories = Category.query.filter(Category.id.in_(category_ids or [])).all()
    from app.models import Tag
    shared_tags = Tag.query.filter(Tag.id.in_(tag_ids or [])).all()
    category_by_name = {c.name.lower(): c for c in Category.query.all()}

    imported = 0
    for row in summary["rows"]:
        if row["result"] != "valid":
            continue
        cats = list(shared_categories)
        for name in re.split(r"[;|]", row.get("categories") or ""):
            cat = category_by_name.get(name.strip().lower())
            if cat and cat not in cats:
                cats.append(cat)
        tags = list(shared_tags)
        for t in get_or_create_tags(re.split(r"[;|]", row.get("tags") or "")):
            if t not in tags:
                tags.append(t)
        client = Client(created_by_id=created_by.id if created_by else None)
        save_client(client, row, cats, tags)
        imported += 1
        if imported % 500 == 0:
            db.session.flush()
    summary["imported"] = imported
    # Re-analysing after import would flag every imported row as a duplicate, so freeze the report now.
    with open(os.path.join(_import_dir(), f"{token}-errors.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write(error_report_csv(summary))
    discard(token)
    return summary


def saved_error_report(token):
    if not TOKEN_RE.match(token or ""):
        return None
    path = os.path.join(_import_dir(), f"{token}-errors.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def error_report_csv(summary):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Line", "Result", "Reason", "Name", "Phone", "Email", "Company", "Location"])
    for r in summary["rows"]:
        if r["result"] == "valid":
            continue
        writer.writerow([r["line"], r["result"], r["reason"], r["full_name"], r["phone"], r["email"],
                         r["company"], r["location"]])
    return out.getvalue()
