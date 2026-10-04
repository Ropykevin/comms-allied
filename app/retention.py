"""Data retention: personal data is only kept as long as it is needed.

- CSV uploads are deleted 24 hours after upload (imports finish in minutes).
- Import error reports are deleted after 7 days.
- Raw webhook payloads (phone numbers, message text) are cleared after 30 days;
  the event record itself is kept for troubleshooting and idempotency.
- Campaign attachments no longer used by any campaign are deleted after 24 hours.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import null

from app.extensions import db
from app.models import Campaign, WebhookEvent
from app.utils import utcnow

log = logging.getLogger(__name__)

UPLOAD_MAX_AGE = timedelta(hours=24)
ERROR_REPORT_MAX_AGE = timedelta(days=7)
WEBHOOK_PAYLOAD_MAX_AGE = timedelta(days=30)


def _older_than(path, age):
    modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).replace(tzinfo=None)
    return utcnow() - modified > age


def _purge_imports():
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "imports")
    removed = 0
    if not os.path.isdir(folder):
        return removed
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        max_age = ERROR_REPORT_MAX_AGE if name.endswith("-errors.csv") else UPLOAD_MAX_AGE
        if os.path.isfile(path) and _older_than(path, max_age):
            os.remove(path)
            removed += 1
    return removed


def _purge_unused_attachments():
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "attachments")
    removed = 0
    if not os.path.isdir(folder):
        return removed
    in_use = {os.path.basename(p) for (p,) in db.session.query(Campaign.attachment_path)
              .filter(Campaign.attachment_path.isnot(None))}
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if name not in in_use and os.path.isfile(path) and _older_than(path, UPLOAD_MAX_AGE):
            os.remove(path)
            removed += 1
    return removed


def _purge_webhook_payloads():
    count = (
        WebhookEvent.query.filter(WebhookEvent.received_at < utcnow() - WEBHOOK_PAYLOAD_MAX_AGE,
                                  WebhookEvent.payload.isnot(None))
        .update({"payload": null()}, synchronize_session=False)
    )
    db.session.commit()
    return count


def purge_old_data():
    result = {
        "import_files": _purge_imports(),
        "attachments": _purge_unused_attachments(),
        "webhook_payloads": _purge_webhook_payloads(),
    }
    log.info("Retention purge: %s", result)
    return result
