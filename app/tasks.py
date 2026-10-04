"""Background job dispatch.

TASK_BACKEND decides where jobs run:
  celery  — Redis queue + Celery worker (production)
  thread  — a daemon thread in the web process (simple development setup)
  sync    — inline, in the current request (tests)

Never send thousands of messages inside a normal HTTP request: callers commit
their changes, then call enqueue(...).
"""
import logging
import threading

from flask import current_app

log = logging.getLogger(__name__)


def _send_campaign(campaign_id):
    from app.campaigns.services import process_campaign
    process_campaign(campaign_id)


def _retry_campaign_failures(campaign_id):
    from app.campaigns.services import retry_failed_messages
    retry_failed_messages(campaign_id)


def _send_message(message_id):
    from app.conversations.services import deliver_message
    deliver_message(message_id)


def _dispatch_due_campaigns():
    from app.campaigns.services import dispatch_due_campaigns
    return dispatch_due_campaigns()


def _send_password_reset(user_id):
    from app.auth.routes import send_password_reset
    send_password_reset(user_id)


def _purge_old_data():
    from app.retention import purge_old_data
    return purge_old_data()


JOBS = {
    "send_campaign": _send_campaign,
    "retry_campaign_failures": _retry_campaign_failures,
    "send_message": _send_message,
    "dispatch_due_campaigns": _dispatch_due_campaigns,
    "send_password_reset": _send_password_reset,
    "purge_old_data": _purge_old_data,
}


def enqueue(job_name, *args):
    backend = current_app.config.get("TASK_BACKEND", "thread")
    if job_name not in JOBS:
        raise ValueError(f"Unknown job {job_name}")
    if backend == "celery":
        current_app.extensions["celery"].send_task(f"allied.{job_name}", args=list(args))
    elif backend == "thread":
        app = current_app._get_current_object()
        threading.Thread(target=_run_in_context, args=(app, job_name, args), daemon=True).start()
    else:
        JOBS[job_name](*args)


def _run_in_context(app, job_name, args):
    with app.app_context():
        try:
            JOBS[job_name](*args)
        except Exception:  # noqa: BLE001
            log.exception("Background job %s%r failed", job_name, args)
            from app.extensions import db
            db.session.rollback()
        finally:
            from app.extensions import db
            db.session.remove()


def init_tasks(app):
    if app.config.get("TASK_BACKEND") == "celery":
        from app.celery_app import celery_init_app
        celery_init_app(app)
