from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import audit
from app.campaigns.forms import TemplateForm
from app.campaigns.services import unknown_variables
from app.extensions import db
from app.models import CHANNEL_EMAIL, CHANNELS, MessageTemplate
from app.permissions import Permission, permission_required

bp = Blueprint("templates", __name__, url_prefix="/templates")


@bp.route("/")
@permission_required(Permission.TEMPLATES_VIEW)
def index():
    channel = request.args.get("channel", "")
    show = request.args.get("show", "active")
    q = MessageTemplate.query
    if channel in CHANNELS:
        q = q.filter_by(channel=channel)
    if show != "all":
        q = q.filter_by(status=MessageTemplate.STATUS_ACTIVE)
    return render_template("campaigns/templates/index.html", templates=q.order_by(MessageTemplate.name).all(),
                           channel=channel, show=show)


def _save(template, form, is_new):
    name = form.name.data.strip()
    clash = MessageTemplate.query.filter(
        db.func.lower(MessageTemplate.name) == name.lower(), MessageTemplate.channel == form.channel.data
    )
    if not is_new:
        clash = clash.filter(MessageTemplate.id != template.id)
    if clash.first():
        form.name.errors.append("A template with this name already exists for that channel.")
        return False
    unknown = unknown_variables(form.content.data + (form.subject.data or ""))
    if unknown:
        form.content.errors.append("Unknown variable(s): " + ", ".join("{{%s}}" % u for u in unknown))
        return False
    if form.channel.data == CHANNEL_EMAIL and not (form.subject.data or "").strip():
        form.subject.errors.append("Email templates need a subject.")
        return False
    template.name = name
    template.channel = form.channel.data
    is_email = form.channel.data == CHANNEL_EMAIL
    template.subject = ((form.subject.data or "").strip() or None) if is_email else None
    template.content = form.content.data
    template.is_html = bool(form.is_html.data) and form.channel.data == CHANNEL_EMAIL
    template.provider_template_name = (form.provider_template_name.data or "").strip() or None
    template.status = form.status.data
    if is_new:
        template.created_by_id = current_user.id
        db.session.add(template)
    db.session.flush()
    audit.record("template.created" if is_new else "template.updated", "template", template.id)
    db.session.commit()
    return True


@bp.route("/new", methods=["GET", "POST"])
@permission_required(Permission.TEMPLATES_MANAGE)
def create():
    form = TemplateForm()
    if request.method == "GET" and request.args.get("channel") in CHANNELS:
        form.channel.data = request.args["channel"]
    if form.validate_on_submit() and _save(MessageTemplate(), form, True):
        flash("Template saved.", "success")
        return redirect(url_for("templates.index"))
    return render_template("campaigns/templates/form.html", form=form, template=None)


@bp.route("/<int:template_id>/edit", methods=["GET", "POST"])
@permission_required(Permission.TEMPLATES_MANAGE)
def edit(template_id):
    template = db.session.get(MessageTemplate, template_id) or abort(404)
    form = TemplateForm(obj=template)
    if form.validate_on_submit() and _save(template, form, False):
        flash("Template updated.", "success")
        return redirect(url_for("templates.index"))
    return render_template("campaigns/templates/form.html", form=form, template=template)


@bp.route("/<int:template_id>/delete", methods=["POST"])
@permission_required(Permission.TEMPLATES_MANAGE)
def delete(template_id):
    template = db.session.get(MessageTemplate, template_id) or abort(404)
    name = template.name
    audit.record("template.deleted", "template", template.id, {"name": name})
    db.session.delete(template)
    db.session.commit()
    flash(f"Template “{name}” deleted.", "success")
    return redirect(url_for("templates.index"))
