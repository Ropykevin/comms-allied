from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import audit
from app.auth.forms import ChangePasswordForm, ForgotPasswordForm, LoginForm, ProfileForm, ResetPasswordForm
from app.extensions import db, limiter
from app.models import User
from app.utils import normalize_email, utcnow

bp = Blueprint("auth", __name__)

RESET_SALT = "password-reset"
RESET_MAX_AGE = 60 * 60  # one hour


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=RESET_SALT)


def generate_reset_token(user):
    # Including part of the hash makes the token single-use: it stops working once the password changes.
    return _serializer().dumps({"uid": user.id, "ph": user.password_hash[-12:]})


def verify_reset_token(token):
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.session.get(User, data.get("uid"))
    if user is None or not user.is_active or user.password_hash[-12:] != data.get("ph"):
        return None
    return user


def _safe_next(target):
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/"):
        return None
    return target


@bp.route("/")
def index():
    return redirect(url_for("dashboard.index"))


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=normalize_email(form.email.data)).first()
        if user is None or not user.check_password(form.password.data):
            flash("Invalid email or password.", "error")
        elif not user.is_active:
            flash("Your account has been deactivated. Contact an administrator.", "error")
        else:
            login_user(user, remember=form.remember.data)
            user.last_login_at = utcnow()
            audit.record("user.logged_in", "user", user.id, user=user)
            db.session.commit()
            return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.index"))
    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.record("user.logged_out", "user", current_user.id)
    db.session.commit()
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=normalize_email(form.email.data)).first()
        if user and user.is_active:
            _send_reset_email(user)
            audit.record("user.password_reset_requested", "user", user.id, user=user)
            db.session.commit()
        # Same response either way so the form can't be used to discover accounts.
        flash("If that email belongs to an account, a reset link is on its way.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html", form=form)


def _send_reset_email(user):
    from app.messaging.service import message_service

    token = generate_reset_token(user)
    link = current_app.config["APP_BASE_URL"] + url_for("auth.reset_password", token=token)
    html = render_template("email/password_reset.html", user=user, link=link)
    text = f"Hello {user.first_name},\n\nReset your password using this link (valid for 1 hour):\n{link}\n"
    message_service.send_transactional_email(user.email, "Reset your password", html, text)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def reset_password(token):
    user = verify_reset_token(token)
    if user is None:
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot_password"))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        audit.record("user.password_reset", "user", user.id, user=user)
        db.session.commit()
        flash("Your password has been reset. You can now sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", form=form)


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    form = ProfileForm(obj=current_user)
    if form.validate_on_submit():
        current_user.full_name = form.full_name.data.strip()
        current_user.phone = (form.phone.data or "").strip() or None
        audit.record("user.profile_updated", "user", current_user.id)
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))
    return render_template("auth/profile.html", form=form, password_form=ChangePasswordForm())


@bp.route("/change-password", methods=["POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Your current password is incorrect.", "error")
        else:
            current_user.set_password(form.password.data)
            audit.record("user.password_changed", "user", current_user.id)
            db.session.commit()
            flash("Password updated.", "success")
    else:
        for errors in form.errors.values():
            for error in errors:
                flash(error, "error")
    return redirect(url_for("auth.profile"))
