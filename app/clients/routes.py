import csv
import io
import re

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import current_user

from app import audit
from app.clients import importer
from app.clients.forms import ClientForm, ImportForm
from app.clients.services import (
    PER_PAGE,
    bulk_apply,
    filtered_clients,
    find_duplicate,
    get_or_create_tags,
    parse_filters,
    save_client,
)
from app.extensions import db
from app.models import (
    CHANNELS,
    Campaign,
    CampaignRecipient,
    Category,
    Client,
    ClientStatus,
    Conversation,
    Tag,
)
from app.permissions import Permission, permission_required

bp = Blueprint("clients", __name__, url_prefix="/clients")


def _form_choices(form):
    form.categories.choices = [(c.id, c.name) for c in Category.query.order_by(Category.name)]
    form.tags.choices = [(t.id, t.name) for t in Tag.query.order_by(Tag.name)]


def _filter_params(filters):
    params = {k: v for k, v in filters.items() if v and k != "sort"}
    if filters.get("sort") and filters["sort"] != "newest":
        params["sort"] = filters["sort"]
    return params


@bp.route("/")
@permission_required(Permission.CLIENTS_VIEW)
def index():
    filters = parse_filters(request.args)
    page = request.args.get("page", 1, type=int)
    clients = filtered_clients(filters).paginate(page=page, per_page=PER_PAGE, error_out=False)
    return render_template(
        "clients/index.html",
        clients=clients,
        filters=filters,
        params=_filter_params(filters),
        categories=Category.query.order_by(Category.name).all(),
        tags=Tag.query.order_by(Tag.name).all(),
        statuses=ClientStatus,
    )


def _form_data(form):
    return {
        "full_name": form.full_name.data, "phone": form.phone.data, "whatsapp": form.whatsapp.data,
        "email": form.email.data, "company": form.company.data, "location": form.location.data,
        "status": form.status.data, "notes": form.notes.data,
        "sms_allowed": form.sms_allowed.data, "whatsapp_allowed": form.whatsapp_allowed.data,
        "email_allowed": form.email_allowed.data,
    }


def _selected_categories_and_tags(form):
    categories = Category.query.filter(Category.id.in_(form.categories.data or [])).all()
    tags = Tag.query.filter(Tag.id.in_(form.tags.data or [])).all()
    for t in get_or_create_tags((form.new_tags.data or "").split(",")):
        if t not in tags:
            tags.append(t)
    return categories, tags


@bp.route("/new", methods=["GET", "POST"])
@permission_required(Permission.CLIENTS_EDIT)
def create():
    form = ClientForm()
    _form_choices(form)
    if request.method == "GET":
        preset = request.args.get("category", type=int)
        form.categories.data = [preset] if preset else []
    if form.validate_on_submit():
        duplicate = find_duplicate(form.phone.data, form.email.data)
        if duplicate:
            flash(f"{duplicate.full_name} already uses this phone number or email.", "error")
        else:
            client = Client(created_by_id=current_user.id)
            categories, tags = _selected_categories_and_tags(form)
            save_client(client, _form_data(form), categories, tags)
            audit.record("client.created", "client", client.id)
            db.session.commit()
            flash(f"{client.full_name} has been added.", "success")
            return redirect(url_for("clients.detail", client_id=client.id))
    return render_template("clients/form.html", form=form, client=None)


@bp.route("/<int:client_id>")
@permission_required(Permission.CLIENTS_VIEW)
def detail(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    conversations = {c.channel: c for c in Conversation.query.filter_by(client_id=client.id)}
    campaign_history = (
        db.session.query(CampaignRecipient, Campaign)
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .filter(CampaignRecipient.client_id == client.id)
        .order_by(CampaignRecipient.created_at.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "clients/detail.html", client=client, conversations=conversations, channels=CHANNELS,
        campaign_history=campaign_history, categories=Category.query.order_by(Category.name).all(),
    )


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@permission_required(Permission.CLIENTS_EDIT)
def edit(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    form = ClientForm(obj=client)
    _form_choices(form)
    if request.method == "GET":
        form.categories.data = [c.id for c in client.categories]
        form.tags.data = [t.id for t in client.tags]
        for ch in CHANNELS:
            cc = client.channel(ch)
            getattr(form, f"{ch}_allowed").data = cc.is_allowed if cc else True
        wa = client.channel("whatsapp")
        form.whatsapp.data = wa.address if wa and wa.address != client.phone else ""
    if form.validate_on_submit():
        duplicate = find_duplicate(form.phone.data, form.email.data, exclude_id=client.id)
        if duplicate:
            flash(f"{duplicate.full_name} already uses this phone number or email.", "error")
        else:
            before = {c.id for c in client.categories}
            categories, tags = _selected_categories_and_tags(form)
            save_client(client, _form_data(form), categories, tags)
            audit.record("client.updated", "client", client.id)
            if before != {c.id for c in categories}:
                audit.record("client.categorized", "client", client.id, {"categories": [c.name for c in categories]})
            db.session.commit()
            flash("Client updated.", "success")
            return redirect(url_for("clients.detail", client_id=client.id))
    return render_template("clients/form.html", form=form, client=client)


@bp.route("/<int:client_id>/archive", methods=["POST"])
@permission_required(Permission.CLIENTS_EDIT)
def archive(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    client.status = ClientStatus.ARCHIVED
    audit.record("client.archived", "client", client.id)
    db.session.commit()
    flash(f"{client.full_name} has been archived and will no longer receive campaigns.", "success")
    return redirect(request.referrer or url_for("clients.index"))


@bp.route("/<int:client_id>/restore", methods=["POST"])
@permission_required(Permission.CLIENTS_EDIT)
def restore(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    client.status = ClientStatus.ACTIVE
    audit.record("client.restored", "client", client.id)
    db.session.commit()
    flash(f"{client.full_name} has been restored.", "success")
    return redirect(url_for("clients.detail", client_id=client.id))


@bp.route("/<int:client_id>/delete", methods=["POST"])
@permission_required(Permission.CLIENTS_DELETE)
def delete(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    name = client.full_name
    audit.record("client.deleted", "client", client.id, {"name": name})
    db.session.delete(client)
    db.session.commit()
    flash(f"{name} and their communication history have been permanently deleted.", "success")
    return redirect(url_for("clients.index"))


@bp.route("/<int:client_id>/channels/<channel>", methods=["POST"])
@permission_required(Permission.CLIENTS_EDIT)
def set_channel_preference(client_id, channel):
    client = db.session.get(Client, client_id) or abort(404)
    if channel not in CHANNELS:
        abort(404)
    cc = client.channel(channel)
    if cc is None:
        abort(400)
    if request.form.get("allowed") == "1":
        cc.opt_in()
        action = "client.opted_in"
    else:
        cc.opt_out("staff")
        action = "client.opted_out"
    audit.record(action, "client", client.id, {"channel": channel, "source": "staff"})
    db.session.commit()
    flash(f"{cc.label} preference updated.", "success")
    return redirect(url_for("clients.detail", client_id=client.id))


@bp.route("/<int:client_id>/categories", methods=["POST"])
@permission_required(Permission.CLIENTS_CATEGORIZE)
def update_categories(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    ids = [int(v) for v in request.form.getlist("category") if v.isdigit()]
    client.categories = Category.query.filter(Category.id.in_(ids)).all()
    audit.record("client.categorized", "client", client.id, {"categories": [c.name for c in client.categories]})
    db.session.commit()
    flash("Categories updated.", "success")
    return redirect(url_for("clients.detail", client_id=client.id))


@bp.route("/bulk", methods=["POST"])
@permission_required(Permission.CLIENTS_CATEGORIZE)
def bulk():
    action = request.form.get("action", "")
    ids = [int(v) for v in request.form.getlist("client_ids") if v.isdigit()]
    if action == "archive" and not current_user.can(Permission.CLIENTS_EDIT):
        abort(403)
    if not ids:
        flash("Select at least one client.", "warning")
        return redirect(request.referrer or url_for("clients.index"))
    target = request.form.get("target_id", type=int)
    count = bulk_apply(ids, action, target)
    if count:
        audit.record(f"client.bulk_{action}", "client", None, {"client_ids": ids[:500], "target_id": target})
        db.session.commit()
        flash(f"Updated {count} client{'s' if count != 1 else ''}.", "success")
    else:
        flash("Nothing was changed. Choose an action and a category or tag.", "warning")
    return redirect(request.referrer or url_for("clients.index"))


def _csv_safe(value):
    """Neutralise spreadsheet formula injection while leaving phone numbers like +2547… intact."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "@", "\t", "\r") or re.match(r"^[+-][^\d\s]", text):
        return "'" + text
    return text


@bp.route("/export")
@permission_required(Permission.CLIENTS_EXPORT)
def export():
    filters = parse_filters(request.args)
    query = filtered_clients(filters)

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Name", "Phone", "WhatsApp", "Email", "Company", "Location", "Status", "Categories",
                         "Tags", "SMS", "WhatsApp Consent", "Email Consent", "Last Contact", "Created"])
        yield buf.getvalue()
        for client in query.yield_per(500):
            buf.seek(0)
            buf.truncate()
            pref = {ch: (client.channel(ch).status if client.channel(ch) else "") for ch in CHANNELS}
            wa = client.channel("whatsapp")
            writer.writerow([_csv_safe(v) for v in [
                client.full_name, client.phone, wa.address if wa else "", client.email, client.company,
                client.location, client.status, "; ".join(c.name for c in client.categories),
                "; ".join(t.name for t in client.tags), pref["sms"], pref["whatsapp"], pref["email"],
                client.last_contacted_at.isoformat() if client.last_contacted_at else "",
                client.created_at.isoformat(),
            ]])
            yield buf.getvalue()

    audit.record("client.exported", "client", None, {"filters": {k: v for k, v in filters.items() if v}})
    db.session.commit()
    return Response(stream_with_context(generate()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=allied-clients.csv"})


# ---- Import ---------------------------------------------------------------------

@bp.route("/import", methods=["GET", "POST"])
@permission_required(Permission.CLIENTS_IMPORT)
def import_upload():
    form = ImportForm()
    if form.validate_on_submit():
        token = importer.store_upload(form.file.data)
        return redirect(url_for("clients.import_preview", token=token))
    return render_template("clients/import_upload.html", form=form)


@bp.route("/import/sample.csv")
@permission_required(Permission.CLIENTS_IMPORT)
def import_sample():
    return Response(importer.SAMPLE_CSV, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=allied-client-import-sample.csv"})


@bp.route("/import/<token>", methods=["GET", "POST"])
@permission_required(Permission.CLIENTS_IMPORT)
def import_preview(token):
    summary = importer.analyze(token)
    if summary is None:
        flash("That import has expired. Please upload the file again.", "error")
        return redirect(url_for("clients.import_upload"))
    if request.method == "POST":
        if summary.get("error"):
            abort(400)
        category_ids = [int(v) for v in request.form.getlist("category") if v.isdigit()]
        tag_ids = [int(v) for v in request.form.getlist("tag") if v.isdigit()]
        result = importer.run_import(token, category_ids, tag_ids, created_by=current_user)
        audit.record("client.imported", "client", None, {
            "imported": result["imported"], "duplicates": result["duplicates"], "invalid": result["invalid"],
        })
        db.session.commit()
        return render_template("clients/import_result.html", summary=result, token=token)
    return render_template(
        "clients/import_preview.html", summary=summary, token=token,
        categories=Category.query.order_by(Category.name).all(), tags=Tag.query.order_by(Tag.name).all(),
    )


@bp.route("/import/<token>/errors.csv")
@permission_required(Permission.CLIENTS_IMPORT)
def import_errors(token):
    report = importer.saved_error_report(token)
    if report is None:
        summary = importer.analyze(token)
        if summary is None:
            abort(404)
        report = importer.error_report_csv(summary)
    return Response(report, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=import-errors.csv"})
