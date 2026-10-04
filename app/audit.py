from flask import has_request_context
from flask_login import current_user

from app.extensions import db
from app.models import AuditLog
from app.utils import client_ip


def record(action, entity_type=None, entity_id=None, details=None, user=None):
    """Add an audit entry to the current session; it is committed with the caller's transaction."""
    user_id = None
    if user is not None:
        user_id = user.id
    elif has_request_context() and current_user and current_user.is_authenticated:
        user_id = current_user.id
    entry = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or None,
        ip_address=client_ip() if has_request_context() else None,
    )
    db.session.add(entry)
    return entry
