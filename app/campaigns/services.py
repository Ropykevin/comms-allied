import logging
import os
import re
import time
import uuid
from types import SimpleNamespace

from flask import current_app
from markupsafe import escape
from sqlalchemy import insert, update
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from app import audit
from app.campaigns.audience import eligible_recipients, normalize_audience
from app.extensions import db
from app.messaging.base import Attachment
from app.messaging.service import message_service
from app.models import (
    CHANNEL_EMAIL,
    CHANNEL_WHATSAPP,
    DIRECTION_OUTBOUND,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Client,
    ClientStatus,
    Message,
    MessageStatus,
)
from app.tasks import enqueue
from app.utils import utcnow

log = logging.getLogger(__name__)

VARIABLE_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
VARIABLES = {
    "first_name": lambda c: c.first_name or "there",
    "last_name": lambda c: c.last_name or "",
    "full_name": lambda c: c.full_name or "",
    "company": lambda c: c.company or "",
    "location": lambda c: c.location or "",
}
ATTACHMENT_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "docx", "xlsx", "pptx", "txt"}

PLACEHOLDER_CLIENT = SimpleNamespace(
    first_name="John", last_name="Kamau", full_name="John Kamau", company="ABC Ltd", location="Nairobi"
)


# ---- personalisation --------------------------------------------------------------

def personalize(text, client, html=False):
    """Replace {{variables}} with the client's details.

    Deliberately a plain substitution (not Jinja) so message content written by
    staff can never execute template code. Unknown variables are left as typed.
    """
    if not text:
        return text or ""

    def repl(match):
        fn = VARIABLES.get(match.group(1))
        if fn is None:
            return match.group(0)
        value = fn(client)
        return str(escape(value)) if html else value

    return VARIABLE_RE.sub(repl, text)


def variables_in(text):
    """Known variables in order of appearance — maps onto WhatsApp template {{1}}, {{2}}…"""
    seen = []
    for name in VARIABLE_RE.findall(text or ""):
        if name in VARIABLES and name not in seen:
            seen.append(name)
    return seen


def unknown_variables(text):
    return sorted({name for name in VARIABLE_RE.findall(text or "") if name not in VARIABLES})


def sms_segments(text):
    text = text or ""
    gsm = all(ord(ch) < 128 for ch in text)
    single, multi = (160, 153) if gsm else (70, 67)
    if len(text) <= single:
        return 1 if text else 0
    return -(-len(text) // multi)


# ---- attachments -----------------------------------------------------------------

def save_attachment(file_storage):
    filename = secure_filename(file_storage.filename or "")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ATTACHMENT_EXTENSIONS:
        raise ValueError(f"Attachments must be one of: {', '.join(sorted(ATTACHMENT_EXTENSIONS))}.")
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "attachments")
    os.makedirs(folder, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(folder, stored))
    return os.path.join("attachments", stored), filename


def _attachments(campaign):
    if campaign.channel != CHANNEL_EMAIL or not campaign.attachment_path:
        return []
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], campaign.attachment_path)
    if not os.path.exists(path):
        log.warning("Attachment for campaign %s is missing on disk", campaign.id)
        return []
    return [Attachment(filename=campaign.attachment_name, path=path)]


def _whatsapp_template(campaign, client):
    template = campaign.template
    if campaign.channel != CHANNEL_WHATSAPP or not template or not template.provider_template_name:
        return None
    params = [VARIABLES[name](client) for name in variables_in(campaign.content)]
    return {"name": template.provider_template_name, "language": "en", "params": params}


# ---- lifecycle --------------------------------------------------------------------

def _claim(campaign, from_statuses, to_status):
    """Atomically move a campaign between statuses; False if another request got there first."""
    result = db.session.execute(
        update(Campaign)
        .where(Campaign.id == campaign.id, Campaign.status.in_(from_statuses))
        .values(status=to_status, updated_at=utcnow())
    )
    if result.rowcount != 1:
        db.session.rollback()
        return False
    db.session.refresh(campaign)
    return True


def snapshot_recipients(campaign):
    """Freeze the audience into campaign_recipients at send time."""
    audience = normalize_audience(campaign.audience)
    rows = eligible_recipients(audience, campaign.channel)
    from app.campaigns.audience import estimate
    total_in_audience = estimate(audience)["total"]
    campaign.recipients.delete()
    now = utcnow()
    if rows:
        db.session.execute(insert(CampaignRecipient), [
            {"campaign_id": campaign.id, "client_id": client_id, "address": address,
             "status": CampaignRecipient.STATUS_PENDING, "created_at": now}
            for client_id, address in rows
        ])
    campaign.total_recipients = len(rows)
    campaign.excluded_count = total_in_audience - len(rows)
    return len(rows)


def queue_campaign(campaign, user=None, from_statuses=(CampaignStatus.DRAFT, CampaignStatus.SCHEDULED)):
    """Snapshot recipients and hand the campaign to a background worker."""
    if not _claim(campaign, from_statuses, CampaignStatus.QUEUED):
        return False
    count = snapshot_recipients(campaign)
    campaign.queued_at = utcnow()
    if user is not None:
        campaign.sent_by_id = user.id
    audit.record("campaign.sent", "campaign", campaign.id, {"recipients": count, "channel": campaign.channel},
                 user=user)
    db.session.commit()
    enqueue("send_campaign", campaign.id)
    return True


def schedule_campaign(campaign, when_utc, user=None):
    campaign.status = CampaignStatus.SCHEDULED
    campaign.scheduled_at = when_utc
    audit.record("campaign.scheduled", "campaign", campaign.id, {"scheduled_at": when_utc.isoformat()}, user=user)


def cancel_campaign(campaign):
    if campaign.status == CampaignStatus.SCHEDULED:
        campaign.status = CampaignStatus.DRAFT
        campaign.scheduled_at = None
        return "unscheduled"
    if campaign.status in (CampaignStatus.QUEUED, CampaignStatus.SENDING):
        campaign.status = CampaignStatus.CANCELLED
        campaign.recipients.filter_by(status=CampaignRecipient.STATUS_PENDING).update(
            {"status": CampaignRecipient.STATUS_SKIPPED, "error_message": "Campaign cancelled"},
            synchronize_session=False,
        )
        campaign.completed_at = utcnow()
        return "cancelled"
    return None


def dispatch_due_campaigns():
    """Queue every scheduled campaign whose time has come. Safe to run from several workers."""
    due = (
        Campaign.query.filter(Campaign.status == CampaignStatus.SCHEDULED, Campaign.scheduled_at <= utcnow())
        .order_by(Campaign.scheduled_at)
        .all()
    )
    dispatched = 0
    for campaign in due:
        if queue_campaign(campaign, from_statuses=(CampaignStatus.SCHEDULED,)):
            dispatched += 1
    return dispatched


def process_campaign(campaign_id):
    """Worker entry point: deliver every pending recipient in batches."""
    campaign = db.session.get(Campaign, campaign_id)
    if campaign is None or campaign.status not in (CampaignStatus.QUEUED, CampaignStatus.SENDING):
        return
    if not _claim(campaign, (CampaignStatus.QUEUED, CampaignStatus.SENDING), CampaignStatus.SENDING):
        return
    campaign.started_at = campaign.started_at or utcnow()
    db.session.commit()

    attachments = _attachments(campaign)
    batch_size = current_app.config["CAMPAIGN_BATCH_SIZE"]
    while True:
        db.session.refresh(campaign)
        if campaign.status == CampaignStatus.CANCELLED:
            return
        batch = (
            campaign.recipients.filter_by(status=CampaignRecipient.STATUS_PENDING)
            .options(selectinload(CampaignRecipient.client).selectinload(Client.contact_channels))
            .order_by(CampaignRecipient.id).limit(batch_size).all()
        )
        if not batch:
            break
        for recipient in batch:
            _deliver(campaign, recipient, attachments)
        db.session.commit()

    _retry_pass(campaign, attachments)
    _finalize(campaign)


def _deliver(campaign, recipient, attachments):
    client = recipient.client
    now = utcnow()
    cc = client.channel(campaign.channel) if client else None
    # Re-check consent at send time: a client may have opted out after the audience was frozen.
    if client is None or client.status == ClientStatus.ARCHIVED or cc is None or not cc.is_reachable:
        recipient.status = CampaignRecipient.STATUS_SKIPPED
        recipient.error_message = "Opted out or archived before sending"
        recipient.processed_at = now
        return

    conversation = message_service.get_or_create_conversation(client, campaign.channel)
    message = Message(
        conversation=conversation,
        campaign_id=campaign.id,
        channel=campaign.channel,
        direction=DIRECTION_OUTBOUND,
        recipient=cc.address,
        subject=personalize(campaign.subject, client) if campaign.channel == CHANNEL_EMAIL else None,
        content=personalize(campaign.content, client, html=campaign.is_html),
        is_html=campaign.is_html and campaign.channel == CHANNEL_EMAIL,
        status=MessageStatus.QUEUED,
        sent_by_id=campaign.sent_by_id,
    )
    db.session.add(message)
    db.session.flush()
    recipient.message_id = message.id
    message_service.send(message, attachments, _whatsapp_template(campaign, client))
    recipient.status = (
        CampaignRecipient.STATUS_FAILED if message.status == MessageStatus.FAILED else CampaignRecipient.STATUS_PROCESSED
    )
    recipient.error_message = message.error_message if message.status == MessageStatus.FAILED else None
    recipient.processed_at = now


def _retry_pass(campaign, attachments):
    """Retry transient provider failures with exponential backoff."""
    backoff = current_app.config.get("RETRY_BACKOFF_SECONDS", 5)
    for attempt in range(current_app.config["MESSAGE_MAX_RETRIES"]):
        waiting = Message.query.filter(
            Message.campaign_id == campaign.id, Message.status == MessageStatus.QUEUED, Message.retry_count > 0
        ).all()
        if not waiting:
            return
        if backoff:
            time.sleep(min(backoff * (2 ** attempt), 120))
        for message in waiting:
            message_service.send(message, attachments, _whatsapp_template(campaign, message.conversation.client))
        db.session.commit()
    # Anything still queued after the final attempt has failed for good.
    for message in Message.query.filter_by(campaign_id=campaign.id, status=MessageStatus.QUEUED).all():
        message.status = MessageStatus.FAILED
        message.failed_at = utcnow()
    db.session.commit()


def _finalize(campaign):
    db.session.refresh(campaign)
    if campaign.status == CampaignStatus.CANCELLED:
        return
    statuses = dict(
        db.session.query(Message.status, db.func.count(Message.id))
        .filter(Message.campaign_id == campaign.id).group_by(Message.status).all()
    )
    total = sum(statuses.values())
    all_failed = total > 0 and statuses.get(MessageStatus.FAILED, 0) == total
    campaign.status = CampaignStatus.FAILED if all_failed else CampaignStatus.COMPLETED
    campaign.completed_at = utcnow()
    audit.record("campaign.completed", "campaign", campaign.id, {"messages": total,
                                                                  "failed": statuses.get(MessageStatus.FAILED, 0)})
    db.session.commit()


def retry_failed_messages(campaign_id):
    """Manually re-send messages that failed (e.g. after fixing provider credentials)."""
    campaign = db.session.get(Campaign, campaign_id)
    if campaign is None:
        return 0
    attachments = _attachments(campaign)
    failed = Message.query.filter_by(campaign_id=campaign.id, status=MessageStatus.FAILED).all()
    for message in failed:
        message.status = MessageStatus.QUEUED
        message.failed_at = None
        message.retry_count = 0
        message_service.send(message, attachments, _whatsapp_template(campaign, message.conversation.client))
        recipient = campaign.recipients.filter_by(message_id=message.id).first()
        if recipient is not None:
            recipient.status = (
                CampaignRecipient.STATUS_FAILED if message.status == MessageStatus.FAILED
                else CampaignRecipient.STATUS_PROCESSED
            )
            recipient.error_message = message.error_message if message.status == MessageStatus.FAILED else None
    db.session.commit()
    _retry_pass(campaign, attachments)
    if campaign.status in (CampaignStatus.FAILED, CampaignStatus.COMPLETED):
        _finalize(campaign)
    return len(failed)


def duplicate_campaign(campaign, user):
    copy = Campaign(
        name=f"{campaign.name} (copy)"[:150], description=campaign.description, channel=campaign.channel,
        audience=dict(campaign.audience or {}), subject=campaign.subject, content=campaign.content,
        is_html=campaign.is_html, template_id=campaign.template_id, timezone=campaign.timezone,
        attachment_path=campaign.attachment_path, attachment_name=campaign.attachment_name,
        status=CampaignStatus.DRAFT, created_by_id=user.id,
    )
    db.session.add(copy)
    db.session.flush()
    audit.record("campaign.created", "campaign", copy.id, {"duplicated_from": campaign.id})
    return copy
