from functools import wraps

from flask import abort
from flask_login import current_user, login_required


class Permission:
    USERS_MANAGE = "users.manage"
    AUDIT_VIEW = "audit.view"
    SETTINGS_VIEW = "settings.view"

    CLIENTS_VIEW = "clients.view"
    CLIENTS_EDIT = "clients.edit"
    CLIENTS_DELETE = "clients.delete"
    CLIENTS_IMPORT = "clients.import"
    CLIENTS_EXPORT = "clients.export"
    CLIENTS_CATEGORIZE = "clients.categorize"

    CATEGORIES_MANAGE = "categories.manage"

    CAMPAIGNS_VIEW = "campaigns.view"
    CAMPAIGNS_MANAGE = "campaigns.manage"
    CAMPAIGNS_SEND = "campaigns.send"

    TEMPLATES_VIEW = "templates.view"
    TEMPLATES_MANAGE = "templates.manage"

    CONVERSATIONS_VIEW = "conversations.view"
    CONVERSATIONS_REPLY = "conversations.reply"
    CONVERSATIONS_ASSIGN = "conversations.assign"

    ANALYTICS_VIEW = "analytics.view"


P = Permission

ALL_PERMISSIONS = {
    value for key, value in vars(Permission).items() if not key.startswith("_") and isinstance(value, str)
}

ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "administrator"
ROLE_MARKETING = "marketing_manager"
ROLE_SUPPORT = "support_agent"
ROLE_VIEWER = "viewer"

ROLE_DEFINITIONS = {
    ROLE_SUPER_ADMIN: ("Super Admin", "Full access to every feature, including users and settings."),
    ROLE_ADMIN: ("Administrator", "Manage clients, categories, campaigns, templates and conversations."),
    ROLE_MARKETING: ("Marketing Manager", "Create campaigns, manage audiences, send campaigns and view analytics."),
    ROLE_SUPPORT: ("Customer Support Agent", "View clients and handle conversations."),
    ROLE_VIEWER: ("Viewer", "Read-only access."),
}

ROLE_PERMISSIONS = {
    ROLE_SUPER_ADMIN: set(ALL_PERMISSIONS),
    ROLE_ADMIN: ALL_PERMISSIONS - {P.USERS_MANAGE},
    ROLE_MARKETING: {
        P.CLIENTS_VIEW, P.CLIENTS_CATEGORIZE, P.CLIENTS_EXPORT, P.CATEGORIES_MANAGE,
        P.CAMPAIGNS_VIEW, P.CAMPAIGNS_MANAGE, P.CAMPAIGNS_SEND,
        P.TEMPLATES_VIEW, P.TEMPLATES_MANAGE,
        P.CONVERSATIONS_VIEW, P.ANALYTICS_VIEW,
    },
    ROLE_SUPPORT: {
        P.CLIENTS_VIEW, P.TEMPLATES_VIEW,
        P.CONVERSATIONS_VIEW, P.CONVERSATIONS_REPLY, P.CONVERSATIONS_ASSIGN,
    },
    ROLE_VIEWER: {
        P.CLIENTS_VIEW, P.CAMPAIGNS_VIEW, P.TEMPLATES_VIEW, P.CONVERSATIONS_VIEW, P.ANALYTICS_VIEW,
    },
}


def permission_required(*permissions):
    """Require an authenticated user holding ALL of the given permissions."""

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not all(current_user.can(p) for p in permissions):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator
