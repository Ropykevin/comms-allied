import logging
import os
import re
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

from flask import current_app
from markupsafe import escape
from sqlalchemy import insert, or_, update
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


# ---- worker lease -------------------------------------------------------------------
# A campaign is processed by at most one worker at a time. The worker holding the lease
# renews it as it goes; if it dies, the lease expires and another worker can resume.

LEASE_SECONDS = 600
INTERRUPTED_ERROR = "Sending was interrupted, so delivery is unknown. Use “Retry failed” to resend."


def _acquire_lease(campaign_id, statuses, new_status=None):
    """Take the campaign's worker lease. Returns a lease token, or None if another worker holds it."""
    token = uuid.uuid4().hex
    now = utcnow()
    values = {"lease_owner": token, "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS)}
    if new_status:
        values["status"] = new_status
    result = db.session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id, Campaign.status.in_(statuses),
               or_(Campaign.lease_expires_at.is_(None), Campaign.lease_expires_at < now))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return token if result.rowcount == 1 else None


def _renew_lease(campaign_id, token):
    """Extend the lease (committed with the caller's next commit). False if the lease was lost."""
    result = db.session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id, Campaign.lease_owner == token)
        .values(lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _release_lease(campaign_id, token):
    db.session.rollback()
    db.session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id, Campaign.lease_owner == token)
        .values(lease_owner=None, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()


def _recover_interrupted(campaign):
    """Settle work left behind by a worker that died mid-send. Never re-sends automatically."""
    now = utcnow()
    in_flight = campaign.recipients.filter(
        CampaignRecipient.status == CampaignRecipient.STATUS_PENDING, CampaignRecipient.message_id.isnot(None)
    ).all()
    for recipient in in_flight:
        message = recipient.message
        if message is not None and message.status in (MessageStatus.SENT, MessageStatus.DELIVERED,
                                                      MessageStatus.READ):
            recipient.status, recipient.error_message = CampaignRecipient.STATUS_PROCESSED, None
        else:
            recipient.status, recipient.error_message = CampaignRecipient.STATUS_FAILED, INTERRUPTED_ERROR
        recipient.processed_at = now
    stuck = Message.query.filter(
        Message.campaign_id == campaign.id,
        or_(Message.status == MessageStatus.SENDING,
            (Message.status == MessageStatus.QUEUED) & (Message.retry_count == 0)),
    ).all()
    for message in stuck:
        message.status, message.failed_at, message.error_message = MessageStatus.FAILED, now, INTERRUPTED_ERROR
    if in_flight or stuck:
        log.warning("Campaign %s: recovered %d interrupted recipients", campaign.id, len(in_flight))


def _still_allowed(message):
    """Consent is re-checked before every retry: the client may have opted out since the first attempt."""
    client = message.conversation.client if message.conversation else None
    cc = client.channel(message.channel) if client else None
    return bool(client and client.status != ClientStatus.ARCHIVED and cc and cc.is_reachable
                and cc.address == message.recipient)


# ---- sending ------------------------------------------------------------------------

def process_campaign(campaign_id):
    """Worker entry point: deliver every pending recipient in batches."""
    token = _acquire_lease(campaign_id, (CampaignStatus.QUEUED, CampaignStatus.SENDING), CampaignStatus.SENDING)
    if token is None:
        return  # cancelled, finished, or another worker is already sending it
    try:
        campaign = db.session.get(Campaign, campaign_id)
        db.session.refresh(campaign)
        campaign.started_at = campaign.started_at or utcnow()
        _recover_interrupted(campaign)
        db.session.commit()

        attachments = _attachments(campaign)
        batch_size = current_app.config["CAMPAIGN_BATCH_SIZE"]
        while True:
            db.session.refresh(campaign)
            if campaign.status == CampaignStatus.CANCELLED:
                return
            batch = (
                campaign.recipients.filter(CampaignRecipient.status == CampaignRecipient.STATUS_PENDING,
                                           CampaignRecipient.message_id.is_(None))
                .options(selectinload(CampaignRecipient.client).selectinload(Client.contact_channels))
                .order_by(CampaignRecipient.id).limit(batch_size).all()
            )
            if not batch:
                break
            for recipient in batch:
                if not _deliver(campaign, recipient, attachments, token):
                    log.warning("Campaign %s: lost the worker lease, stopping", campaign_id)
                    return

        if _retry_pass(campaign, attachments, token):
            _finalize(campaign)
    finally:
        _release_lease(campaign_id, token)


def _deliver(campaign, recipient, attachments, token):
    """Send to one recipient. Returns False if this worker no longer holds the campaign lease."""
    client = recipient.client
    now = utcnow()
    cc = client.channel(campaign.channel) if client else None
    # Re-check consent at send time: a client may have opted out after the audience was frozen.
    if client is None or client.status == ClientStatus.ARCHIVED or cc is None or not cc.is_reachable:
        recipient.status = CampaignRecipient.STATUS_SKIPPED
        recipient.error_message = "Opted out or archived before sending"
        recipient.processed_at = now
        if not _renew_lease(campaign.id, token):
            db.session.rollback()
            return False
        db.session.commit()
        return True

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
    if not _renew_lease(campaign.id, token):
        db.session.rollback()
        return False
    # Record the message before contacting the provider, so a crash mid-send is detected
    # by _recover_interrupted instead of the client being messaged twice.
    db.session.commit()

    message_service.send(message, attachments, _whatsapp_template(campaign, client))
    recipient.status = (
        CampaignRecipient.STATUS_FAILED if message.status == MessageStatus.FAILED else CampaignRecipient.STATUS_PROCESSED
    )
    recipient.error_message = message.error_message if message.status == MessageStatus.FAILED else None
    recipient.processed_at = utcnow()
    db.session.commit()
    return True


def _resend(campaign, message, attachments):
    """Retry one message if the client still consents. Returns False if it was not sent."""
    if not _still_allowed(message):
        message.status, message.failed_at = MessageStatus.FAILED, utcnow()
        message.error_message = "Not retried: the client opted out, was archived or changed their contact details."
        return False
    message_service.send(message, attachments, _whatsapp_template(campaign, message.conversation.client))
    return True


def _retry_pass(campaign, attachments, token):
    """Retry transient provider failures with exponential backoff. False if the lease was lost."""
    backoff = current_app.config.get("RETRY_BACKOFF_SECONDS", 5)
    for attempt in range(current_app.config["MESSAGE_MAX_RETRIES"]):
        waiting = Message.query.filter(
            Message.campaign_id == campaign.id, Message.status == MessageStatus.QUEUED, Message.retry_count > 0
        ).all()
        if not waiting:
            break
        if backoff:
            time.sleep(min(backoff * (2 ** attempt), 120))
        for message in waiting:
            if not _renew_lease(campaign.id, token):
                db.session.rollback()
                return False
            _resend(campaign, message, attachments)
            db.session.commit()
    # Anything still queued after the final attempt has failed for good.
    for message in Message.query.filter_by(campaign_id=campaign.id, status=MessageStatus.QUEUED).all():
        message.status = MessageStatus.FAILED
        message.failed_at = utcnow()
    db.session.commit()
    return True


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
    # The lease also stops a double-clicked "Retry failed" from sending everything twice.
    token = _acquire_lease(campaign_id, (CampaignStatus.COMPLETED, CampaignStatus.FAILED))
    if token is None:
        return 0
    try:
        campaign = db.session.get(Campaign, campaign_id)
        attachments = _attachments(campaign)
        failed_ids = [m.id for m in Message.query.filter_by(campaign_id=campaign.id, status=MessageStatus.FAILED)]
        retried = 0
        for message_id in failed_ids:
            message = db.session.get(Message, message_id)
            if not _renew_lease(campaign.id, token):
                db.session.rollback()
                return retried
            if not _still_allowed(message):
                message.error_message = ("Not retried: the client opted out, was archived or changed their "
                                         "contact details.")
                db.session.commit()
                continue
            message.status, message.failed_at, message.retry_count = MessageStatus.QUEUED, None, 0
            db.session.commit()
            message_service.send(message, attachments, _whatsapp_template(campaign, message.conversation.client))
            recipient = campaign.recipients.filter_by(message_id=message.id).first()
            if recipient is not None:
                recipient.status = (
                    CampaignRecipient.STATUS_FAILED if message.status == MessageStatus.FAILED
                    else CampaignRecipient.STATUS_PROCESSED
                )
                recipient.error_message = message.error_message if message.status == MessageStatus.FAILED else None
            db.session.commit()
            retried += 1
        if _retry_pass(campaign, attachments, token):
            _finalize(campaign)
        return retried
    finally:
        _release_lease(campaign_id, token)


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
