from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from app import audit
from app.extensions import db
from app.models import AuditLog, Role, User, WebhookEvent
from app.permissions import ROLE_PERMISSIONS, ROLE_SUPER_ADMIN, Permission, permission_required
from app.settings.forms import UserForm
from app.utils import normalize_email

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/users")
@permission_required(Permission.USERS_MANAGE)
def users():
    all_users = User.query.join(Role).order_by(User.full_name).all()
    roles = Role.query.order_by(Role.id).all()
    return render_template("settings/users.html", users=all_users, roles=roles, role_permissions=ROLE_PERMISSIONS)


def _user_form(user=None):
    form = UserForm(obj=user)
    form.role_id.choices = [(r.id, r.label) for r in Role.query.order_by(Role.id).all()]
    if user is not None and request.method == "GET":
        form.is_active.data = user.is_active_user
    return form


@bp.route("/users/new", methods=["GET", "POST"])
@permission_required(Permission.USERS_MANAGE)
def user_create():
    form = _user_form()
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        if User.query.filter_by(email=email).first():
            form.email.errors.append("A user with this email already exists.")
        elif not form.password.data:
            form.password.errors.append("Set an initial password for the new user.")
        else:
            user = User(email=email, full_name=form.full_name.data.strip(), phone=form.phone.data or None,
                        role_id=form.role_id.data, is_active_user=form.is_active.data)
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()
            audit.record("user.created", "user", user.id, {"role_id": user.role_id})
            db.session.commit()
            flash(f"{user.full_name} can now sign in.", "success")
            return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", form=form, user=None)


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@permission_required(Permission.USERS_MANAGE)
def user_edit(user_id):
    user = db.session.get(User, user_id) or abort(404)
    form = _user_form(user)
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        clash = User.query.filter(User.email == email, User.id != user.id).first()
        new_role = db.session.get(Role, form.role_id.data)
        demoting_last_admin = (
            user.has_role(ROLE_SUPER_ADMIN)
            and (new_role.name != ROLE_SUPER_ADMIN or not form.is_active.data)
            and User.query.join(Role).filter(Role.name == ROLE_SUPER_ADMIN, User.is_active_user.is_(True)).count() <= 1
        )
        if clash:
            form.email.errors.append("A user with this email already exists.")
        elif demoting_last_admin:
            flash("At least one active Super Admin is required.", "error")
        else:
            changes = {}
            if user.role_id != form.role_id.data:
                changes["role"] = [user.role.name, new_role.name]
            user.full_name = form.full_name.data.strip()
            user.email = email
            user.phone = form.phone.data or None
            user.role_id = form.role_id.data
            user.is_active_user = form.is_active.data
            if form.password.data:
                user.set_password(form.password.data)
                changes["password_reset_by_admin"] = True
            audit.record("user.updated", "user", user.id, changes)
            db.session.commit()
            flash("User updated.", "success")
            return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", form=form, user=user)


@bp.route("/channels")
@permission_required(Permission.SETTINGS_VIEW)
def channels():
    cfg = current_app.config
    channel_info = [
        {
            "key": "sms", "label": "SMS", "provider": cfg["SMS_PROVIDER"],
            "configured": cfg["SMS_PROVIDER"] == "console" or bool(cfg["SMS_API_KEY"] and cfg["SMS_USERNAME"]),
            "webhook": url_for("webhooks.sms", _external=True) + "?token=<SMS_WEBHOOK_TOKEN>",
            "verification": bool(cfg["SMS_WEBHOOK_TOKEN"]),
        },
        {
            "key": "whatsapp", "label": "WhatsApp", "provider": cfg["WHATSAPP_PROVIDER"],
            "configured": cfg["WHATSAPP_PROVIDER"] == "console"
            or bool(cfg["WHATSAPP_API_KEY"] and cfg["WHATSAPP_PHONE_NUMBER_ID"]),
            "webhook": url_for("webhooks.whatsapp", _external=True),
            "verification": bool(cfg["WHATSAPP_APP_SECRET"] and cfg["WHATSAPP_VERIFY_TOKEN"]),
        },
        {
            "key": "email", "label": "Email", "provider": cfg["MAIL_PROVIDER"],
            "configured": cfg["MAIL_PROVIDER"] == "console" or bool(cfg["MAIL_SERVER"]),
            "webhook": url_for("webhooks.email", _external=True),
            "verification": bool(cfg["EMAIL_WEBHOOK_SECRET"]),
        },
    ]
    events = WebhookEvent.query.order_by(WebhookEvent.received_at.desc()).limit(25).all()
    return render_template(
        "settings/channels.html", channels=channel_info, events=events, task_backend=cfg["TASK_BACKEND"]
    )


@bp.route("/audit-log")
@permission_required(Permission.AUDIT_VIEW)
def audit_log():
    page = request.args.get("page", 1, type=int)
    action = request.args.get("action", "").strip()
    q = AuditLog.query
    if action:
        q = q.filter(AuditLog.action.ilike(f"%{action}%"))
    entries = q.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template("settings/audit_log.html", entries=entries, action=action)
