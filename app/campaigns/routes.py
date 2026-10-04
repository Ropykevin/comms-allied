from datetime import datetime

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app import audit
from app.analytics import campaign_analytics
from app.campaigns import services
from app.campaigns.audience import audience_from_form, estimate, normalize_audience, sample_client
from app.campaigns.forms import CampaignForm
from app.clients.services import category_counts
from app.extensions import db
from app.models import (
    CHANNEL_EMAIL,
    CHANNELS,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Category,
    ClientStatus,
    MessageTemplate,
    Tag,
)
from app.permissions import Permission, permission_required
from app.tasks import enqueue
from app.utils import local_to_utc, utcnow

bp = Blueprint("campaigns", __name__, url_prefix="/campaigns")

STEP_FOR_FIELD = {"name": 1, "description": 1, "channel": 3, "subject": 4, "content": 4, "attachment": 4,
                  "schedule_date": 5, "schedule_time": 5, "timezone": 5, "audience": 2}


@bp.route("/")
@permission_required(Permission.CAMPAIGNS_VIEW)
def index():
    page = request.args.get("page", 1, type=int)
    status = request.args.get("status", "")
    channel = request.args.get("channel", "")
    q = Campaign.query
    if status in CampaignStatus.ALL:
        q = q.filter(Campaign.status == status)
    if channel in CHANNELS:
        q = q.filter(Campaign.channel == channel)
    campaigns = q.order_by(Campaign.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    category_names = dict(db.session.query(Category.id, Category.name).all())
    return render_template("campaigns/index.html", campaigns=campaigns, status=status, channel=channel,
                           statuses=CampaignStatus, category_names=category_names)


def _template_choices():
    templates = MessageTemplate.query.filter_by(status=MessageTemplate.STATUS_ACTIVE).order_by(MessageTemplate.name)
    return [(0, "— Start from scratch —")] + [(t.id, f"{t.name} ({t.channel_label})") for t in templates]


def _wizard_context(form, campaign, audience, errors=None, step=1):
    templates = MessageTemplate.query.filter_by(status=MessageTemplate.STATUS_ACTIVE).order_by(MessageTemplate.name)
    return dict(
        form=form,
        campaign=campaign,
        audience=audience,
        categories=Category.query.order_by(Category.name).all(),
        tags=Tag.query.order_by(Tag.name).all(),
        statuses=[s for s in ClientStatus.ALL if s != ClientStatus.ARCHIVED],
        status_labels=ClientStatus.LABELS,
        templates=templates.all(),
        initial_estimate=estimate(audience),
        category_counts=category_counts(),
        errors=errors or [],
        initial_step=step,
    )


def _apply_form(campaign, form, audience):
    campaign.name = form.name.data.strip()
    campaign.description = (form.description.data or "").strip() or None
    campaign.channel = form.channel.data or None
    campaign.audience = audience
    template_id = form.template_id.data or None
    campaign.template_id = template_id if template_id and db.session.get(MessageTemplate, template_id) else None
    campaign.subject = (form.subject.data or "").strip() or None
    campaign.content = form.content.data or None
    campaign.is_html = bool(form.is_html.data) and campaign.channel == CHANNEL_EMAIL
    campaign.timezone = form.timezone.data or "Africa/Nairobi"
    if form.remove_attachment.data:
        campaign.attachment_path = campaign.attachment_name = None


def _validate_for_sending(campaign, action, form):
    """Errors as (field, message). Drafts only need a name; sending needs a complete campaign."""
    errors = []
    if action == "save_draft":
        return errors, None
    if not campaign.channel:
        errors.append(("channel", "Choose a channel: SMS, WhatsApp or Email."))
    if not (campaign.content or "").strip():
        errors.append(("content", "Write the message you want to send."))
    if campaign.channel == CHANNEL_EMAIL and not campaign.subject:
        errors.append(("subject", "Email campaigns need a subject line."))
    unknown = services.unknown_variables((campaign.content or "") + (campaign.subject or ""))
    if unknown:
        errors.append(("content", "Unknown variable(s): " + ", ".join("{{%s}}" % u for u in unknown)))
    if campaign.channel and not errors:
        available = estimate(campaign.audience)["channels"][campaign.channel]["available"]
        if available == 0:
            errors.append(("audience", "No clients in this audience can be reached on the chosen channel."))
    when = None
    if action == "schedule":
        if not form.schedule_date.data or not form.schedule_time.data:
            errors.append(("schedule_date", "Pick a date and time to schedule the campaign."))
        else:
            local = datetime.combine(form.schedule_date.data, form.schedule_time.data)
            when = local_to_utc(local, campaign.timezone)
            if when <= utcnow():
                errors.append(("schedule_date", "The scheduled time must be in the future."))
    return errors, when


def _handle_wizard(campaign):
    is_new = campaign is None
    form = CampaignForm(obj=campaign)
    form.template_id.choices = _template_choices()

    if request.method == "GET":
        if campaign is not None:
            audience = normalize_audience(campaign.audience)
            form.template_id.data = campaign.template_id or 0
            if campaign.scheduled_at:
                from app.utils import to_local
                local = to_local(campaign.scheduled_at, campaign.timezone)
                form.schedule_date.data, form.schedule_time.data = local.date(), local.time().replace(second=0)
        else:
            preset = request.args.get("category", type=int)
            audience = normalize_audience({"category_ids": [preset] if preset else []})
            template = db.session.get(MessageTemplate, request.args.get("template", type=int) or 0)
            if template:
                form.template_id.data = template.id
                form.channel.data = template.channel
                form.subject.data = template.subject
                form.content.data = template.content
                form.is_html.data = template.is_html
        return render_template("campaigns/wizard.html", **_wizard_context(form, campaign, audience))

    audience = audience_from_form(request.form)
    action = request.form.get("action", "save_draft")
    if action not in ("save_draft", "send_now", "schedule"):
        abort(400)
    if action in ("send_now", "schedule") and not current_user.can(Permission.CAMPAIGNS_SEND):
        abort(403)

    if not form.validate_on_submit():
        errors = [(name, msg) for name, msgs in form.errors.items() for msg in msgs]
        step = min(STEP_FOR_FIELD.get(f, 1) for f, _ in errors) if errors else 1
        return render_template("campaigns/wizard.html", **_wizard_context(form, campaign, audience, errors, step)), 400

    if is_new:
        campaign = Campaign(created_by_id=current_user.id, status=CampaignStatus.DRAFT)
        db.session.add(campaign)
    _apply_form(campaign, form, audience)

    upload = request.files.get("attachment")
    if upload and upload.filename and campaign.channel == CHANNEL_EMAIL:
        try:
            campaign.attachment_path, campaign.attachment_name = services.save_attachment(upload)
        except ValueError as exc:
            db.session.rollback()
            return render_template("campaigns/wizard.html", **_wizard_context(
                form, None if is_new else campaign, audience, [("attachment", str(exc))], 4)), 400

    errors, when = _validate_for_sending(campaign, action, form)
    if errors:
        db.session.rollback()
        step = min(STEP_FOR_FIELD.get(f, 1) for f, _ in errors)
        return render_template("campaigns/wizard.html", **_wizard_context(
            form, None if is_new else db.session.get(Campaign, campaign.id), audience, errors, step)), 400

    if campaign.status == CampaignStatus.SCHEDULED and action == "save_draft":
        campaign.status, campaign.scheduled_at = CampaignStatus.DRAFT, None
    db.session.flush()
    audit.record("campaign.created" if is_new else "campaign.updated", "campaign", campaign.id)

    if action == "schedule":
        services.schedule_campaign(campaign, when)
        db.session.commit()
        flash(f"“{campaign.name}” is scheduled.", "success")
    elif action == "send_now":
        db.session.commit()
        if services.queue_campaign(campaign, current_user):
            flash(f"“{campaign.name}” is on its way to {campaign.total_recipients:,} recipients.", "success")
        else:
            flash("This campaign is already being sent.", "warning")
    else:
        db.session.commit()
        flash("Draft saved.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=campaign.id))


@bp.route("/create", methods=["GET", "POST"])
@permission_required(Permission.CAMPAIGNS_MANAGE)
def create():
    return _handle_wizard(None)


@bp.route("/<int:campaign_id>/edit", methods=["GET", "POST"])
@permission_required(Permission.CAMPAIGNS_MANAGE)
def edit(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    if not campaign.is_editable:
        flash("Only draft or scheduled campaigns can be edited.", "warning")
        return redirect(url_for("campaigns.detail", campaign_id=campaign.id))
    return _handle_wizard(campaign)


@bp.route("/<int:campaign_id>")
@permission_required(Permission.CAMPAIGNS_VIEW)
def detail(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    page = request.args.get("page", 1, type=int)
    recipient_status = request.args.get("delivery", "")
    recipients_q = campaign.recipients.options(
        selectinload(CampaignRecipient.client), selectinload(CampaignRecipient.message)
    )
    if recipient_status:
        from app.models import Message
        if recipient_status == "skipped":
            recipients_q = recipients_q.filter(CampaignRecipient.status == CampaignRecipient.STATUS_SKIPPED)
        else:
            recipients_q = recipients_q.join(Message, Message.id == CampaignRecipient.message_id).filter(
                Message.status == recipient_status)
    recipients = recipients_q.order_by(CampaignRecipient.id).paginate(page=page, per_page=25, error_out=False)
    audience = normalize_audience(campaign.audience)
    sample = sample_client(audience, campaign.channel) or services.PLACEHOLDER_CLIENT
    return render_template(
        "campaigns/detail.html",
        campaign=campaign,
        stats=campaign_analytics(campaign),
        recipients=recipients,
        recipient_status=recipient_status,
        audience=audience,
        audience_estimate=estimate(audience) if campaign.is_editable else None,
        categories={c.id: c for c in Category.query.filter(Category.id.in_(audience["category_ids"] or [0]))},
        tags={t.id: t for t in Tag.query.filter(Tag.id.in_(audience["tag_ids"] or [0]))},
        preview_subject=services.personalize(campaign.subject, sample),
        preview_content=services.personalize(campaign.content, sample, html=campaign.is_html),
        sample=sample,
    )


@bp.route("/<int:campaign_id>/stats.json")
@permission_required(Permission.CAMPAIGNS_VIEW)
def stats_json(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    return jsonify(status=campaign.status, status_label=campaign.status_label, stats=campaign_analytics(campaign))


@bp.route("/<int:campaign_id>/send", methods=["POST"])
@permission_required(Permission.CAMPAIGNS_SEND)
def send_now(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    errors, _ = _validate_for_sending(campaign, "send_now", CampaignForm(meta={"csrf": False}))
    if errors:
        for _, msg in errors:
            flash(msg, "error")
        return redirect(url_for("campaigns.detail", campaign_id=campaign.id))
    if services.queue_campaign(campaign, current_user):
        flash(f"“{campaign.name}” is on its way to {campaign.total_recipients:,} recipients.", "success")
    else:
        flash("This campaign can't be sent in its current state.", "warning")
    return redirect(url_for("campaigns.detail", campaign_id=campaign.id))


@bp.route("/<int:campaign_id>/cancel", methods=["POST"])
@permission_required(Permission.CAMPAIGNS_SEND)
def cancel(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    outcome = services.cancel_campaign(campaign)
    if outcome:
        audit.record(f"campaign.{outcome}", "campaign", campaign.id)
        db.session.commit()
        flash("Campaign unscheduled and returned to drafts." if outcome == "unscheduled"
              else "Campaign cancelled. Messages already sent cannot be recalled.", "success")
    else:
        flash("This campaign can no longer be cancelled.", "warning")
    return redirect(url_for("campaigns.detail", campaign_id=campaign.id))


@bp.route("/<int:campaign_id>/retry", methods=["POST"])
@permission_required(Permission.CAMPAIGNS_SEND)
def retry_failed(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    if campaign.status not in (CampaignStatus.COMPLETED, CampaignStatus.FAILED):
        flash("Failed messages can be retried once the campaign has finished sending.", "warning")
        return redirect(url_for("campaigns.detail", campaign_id=campaign.id))
    audit.record("campaign.retry_failed", "campaign", campaign.id)
    db.session.commit()
    enqueue("retry_campaign_failures", campaign.id)
    flash("Retrying failed messages in the background.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=campaign.id))


@bp.route("/<int:campaign_id>/duplicate", methods=["POST"])
@permission_required(Permission.CAMPAIGNS_MANAGE)
def duplicate(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    copy = services.duplicate_campaign(campaign, current_user)
    db.session.commit()
    flash("Campaign duplicated as a new draft.", "success")
    return redirect(url_for("campaigns.edit", campaign_id=copy.id))


@bp.route("/<int:campaign_id>/delete", methods=["POST"])
@permission_required(Permission.CAMPAIGNS_MANAGE)
def delete(campaign_id):
    campaign = db.session.get(Campaign, campaign_id) or abort(404)
    if campaign.status != CampaignStatus.DRAFT:
        flash("Only drafts can be deleted.", "warning")
        return redirect(url_for("campaigns.detail", campaign_id=campaign.id))
    audit.record("campaign.deleted", "campaign", campaign.id, {"name": campaign.name})
    db.session.delete(campaign)
    db.session.commit()
    flash("Draft deleted.", "success")
    return redirect(url_for("campaigns.index"))


# ---- AJAX helpers for the wizard ---------------------------------------------------

@bp.route("/audience-estimate", methods=["POST"])
@permission_required(Permission.CAMPAIGNS_MANAGE)
def audience_estimate():
    data = request.get_json(silent=True) or {}
    return jsonify(estimate(normalize_audience(data)))


@bp.route("/preview", methods=["POST"])
@permission_required(Permission.CAMPAIGNS_MANAGE)
def preview():
    data = request.get_json(silent=True) or {}
    channel = data.get("channel") if data.get("channel") in CHANNELS else None
    is_html = bool(data.get("is_html")) and channel == CHANNEL_EMAIL
    client = sample_client(normalize_audience(data.get("audience")), channel)
    sample = client or services.PLACEHOLDER_CLIENT
    subject = services.personalize(data.get("subject") or "", sample)
    content = services.personalize(data.get("content") or "", sample, html=is_html)
    result = {
        "client_name": sample.full_name,
        "is_sample": client is None,
        "subject": subject,
        "content": content,
        "unknown_variables": services.unknown_variables((data.get("content") or "") + (data.get("subject") or "")),
        "sms_segments": services.sms_segments(content) if channel == "sms" else None,
    }
    if channel == CHANNEL_EMAIL:
        from markupsafe import Markup, escape
        body = Markup(content) if is_html else escape(content).replace("\n", Markup("<br>"))
        result["email_html"] = render_template("email/campaign.html", subject=subject, body_html=body,
                                               unsubscribe_url="#", tracking_url=None)
    return jsonify(result)


@bp.route("/templates/<int:template_id>.json")
@permission_required(Permission.TEMPLATES_VIEW)
def template_json(template_id):
    t = db.session.get(MessageTemplate, template_id) or abort(404)
    return jsonify(id=t.id, name=t.name, channel=t.channel, subject=t.subject, content=t.content, is_html=t.is_html)
