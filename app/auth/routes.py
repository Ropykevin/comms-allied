import time
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from app import audit
from app.auth import mfa
from app.auth.forms import (
    ChangePasswordForm,
    DisableTwoFactorForm,
    ForgotPasswordForm,
    LoginForm,
    ProfileForm,
    ResetPasswordForm,
    TwoFactorForm,
)
from app.extensions import db, limiter
from app.models import User
from app.tasks import enqueue
from app.utils import normalize_email, utcnow

bp = Blueprint("auth", __name__)

RESET_SALT = "password-reset"
RESET_MAX_AGE = 60 * 60  # one hour
MFA_PENDING_SECONDS = 5 * 60
MFA_MAX_ATTEMPTS = 5

_dummy_hash = None


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
    """Only allow redirects to paths on this site (blocks //evil.com, /\\evil.com and similar)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in target):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


def _check_password_timing_safe(user, password):
    """Spend the same time hashing whether or not the account exists, so timing can't reveal accounts."""
    global _dummy_hash
    if user is None:
        if _dummy_hash is None:
            _dummy_hash = generate_password_hash("timing-equaliser",
                                                 method=current_app.config.get("PASSWORD_HASH_METHOD", "scrypt"))
        check_password_hash(_dummy_hash, password or "")
        return False
    return user.check_password(password)


def _login_email_key():
    return "login:" + (normalize_email(request.form.get("email")) or "-")


def _complete_login(user, remember):
    session.clear()
    login_user(user, remember=remember)
    session.permanent = True
    user.last_login_at = utcnow()


@bp.route("/")
def index():
    return redirect(url_for("dashboard.index"))


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
@limiter.limit("20/hour", methods=["POST"], key_func=_login_email_key)
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = LoginForm()
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        user = User.query.filter_by(email=email).first()
        if not _check_password_timing_safe(user, form.password.data):
            audit.record("user.login_failed", "user", user.id if user else None, {"email": (email or "")[:255]})
            db.session.commit()
            flash("Invalid email or password.", "error")
        elif not user.is_active:
            audit.record("user.login_blocked", "user", user.id, {"reason": "deactivated"})
            db.session.commit()
            flash("Your account has been deactivated. Contact an administrator.", "error")
        elif user.mfa_enabled:
            session.clear()
            session["mfa_pending"] = {
                "uid": user.id, "sv": user.session_version, "remember": bool(form.remember.data),
                "next": _safe_next(request.args.get("next")), "at": int(time.time()), "tries": 0,
            }
            return redirect(url_for("auth.login_two_factor"))
        else:
            next_url = _safe_next(request.args.get("next"))
            _complete_login(user, form.remember.data)
            audit.record("user.logged_in", "user", user.id, user=user)
            db.session.commit()
            return redirect(next_url or url_for("dashboard.index"))
    return render_template("auth/login.html", form=form)


def _mfa_user_key():
    return "mfa:" + str((session.get("mfa_pending") or {}).get("uid", "-"))


@bp.route("/login/two-factor", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
@limiter.limit("10/hour", methods=["POST"], key_func=_mfa_user_key)
def login_two_factor():
    pending = session.get("mfa_pending")
    if not pending or time.time() - pending.get("at", 0) > MFA_PENDING_SECONDS:
        session.pop("mfa_pending", None)
        flash("Please sign in again.", "info")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pending["uid"])
    if user is None or not user.is_active or user.session_version != pending["sv"] or not user.mfa_enabled:
        session.pop("mfa_pending", None)
        return redirect(url_for("auth.login"))

    form = TwoFactorForm()
    if form.validate_on_submit():
        step = mfa.verify(user.totp_secret, form.code.data, user.totp_last_step)
        if step is None:
            pending["tries"] = pending.get("tries", 0) + 1
            session["mfa_pending"] = pending
            audit.record("user.mfa_failed", "user", user.id, user=user)
            db.session.commit()
            if pending["tries"] >= MFA_MAX_ATTEMPTS:
                session.pop("mfa_pending", None)
                flash("Too many incorrect codes. Please sign in again.", "error")
                return redirect(url_for("auth.login"))
            flash("That code is incorrect or has expired.", "error")
        else:
            user.totp_last_step = step
            next_url = pending.get("next")
            _complete_login(user, pending.get("remember", False))
            audit.record("user.logged_in", "user", user.id, {"mfa": True}, user=user)
            db.session.commit()
            return redirect(next_url or url_for("dashboard.index"))
    return render_template("auth/login_two_factor.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.record("user.logged_out", "user", current_user.id)
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
@limiter.limit("10/hour", methods=["POST"], key_func=_login_email_key)
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=normalize_email(form.email.data)).first()
        if user and user.is_active:
            audit.record("user.password_reset_requested", "user", user.id, user=user)
            db.session.commit()
            # Sent in the background so response time doesn't reveal whether the account exists.
            enqueue("send_password_reset", user.id)
        flash("If that email belongs to an account, a reset link is on its way.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html", form=form)


def send_password_reset(user_id):
    """Background job: email a password reset link."""
    from app.messaging.service import message_service

    user = db.session.get(User, user_id)
    if user is None or not user.is_active:
        return
    token = generate_reset_token(user)
    base = current_app.config["APP_BASE_URL"]
    with current_app.test_request_context(base_url=base):
        link = base + url_for("auth.reset_password", token=token)
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
        user.set_password(form.password.data)  # also signs the user out everywhere
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
    return render_template("auth/profile.html", form=form, password_form=ChangePasswordForm(),
                           disable_form=DisableTwoFactorForm())


@bp.route("/change-password", methods=["POST"])
@login_required
@limiter.limit("10/hour")
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            audit.record("user.password_change_failed", "user", current_user.id)
            db.session.commit()
            flash("Your current password is incorrect.", "error")
        else:
            user = current_user._get_current_object()
            user.set_password(form.password.data)  # signs out every other session
            audit.record("user.password_changed", "user", user.id)
            db.session.commit()
            _complete_login(user, remember=False)
            db.session.commit()
            flash("Password updated. You have been signed out on your other devices.", "success")
    else:
        for errors in form.errors.values():
            for error in errors:
                flash(error, "error")
    return redirect(url_for("auth.profile"))


# ---- two-factor enrolment ----------------------------------------------------------

@bp.route("/profile/two-factor", methods=["GET", "POST"])
@login_required
@limiter.limit("10/minute", methods=["POST"])
def two_factor_setup():
    user = current_user._get_current_object()
    if user.mfa_enabled:
        return redirect(url_for("auth.profile"))
    secret = session.get("mfa_setup_secret")
    if not secret:
        secret = session["mfa_setup_secret"] = mfa.new_secret()
    form = TwoFactorForm()
    if form.validate_on_submit():
        step = mfa.verify(secret, form.code.data)
        if step is None:
            flash("That code didn't match. Check your phone's clock and try the newest code.", "error")
        else:
            user.totp_secret, user.mfa_enabled, user.totp_last_step = secret, True, step
            user.revoke_sessions()
            audit.record("user.mfa_enabled", "user", user.id)
            db.session.commit()
            _complete_login(user, remember=False)
            db.session.commit()
            flash("Two-factor authentication is on. You'll be asked for a code each time you sign in.", "success")
            return redirect(url_for("dashboard.index"))
    uri = mfa.provisioning_uri(secret, user.email)
    return render_template("auth/two_factor_setup.html", form=form, secret=secret, qr=mfa.qr_svg_data_uri(uri))


@bp.route("/profile/two-factor/disable", methods=["POST"])
@login_required
@limiter.limit("5/minute")
def two_factor_disable():
    user = current_user._get_current_object()
    if not user.mfa_enabled:
        return redirect(url_for("auth.profile"))
    if user.mfa_required:
        abort(403)
    form = DisableTwoFactorForm()
    if (form.validate_on_submit() and user.check_password(form.password.data)
            and mfa.verify(user.totp_secret, form.code.data, user.totp_last_step) is not None):
        user.mfa_enabled, user.totp_secret, user.totp_last_step = False, None, None
        user.revoke_sessions()
        audit.record("user.mfa_disabled", "user", user.id)
        db.session.commit()
        _complete_login(user, remember=False)
        db.session.commit()
        flash("Two-factor authentication is off.", "success")
    else:
        audit.record("user.mfa_disable_failed", "user", user.id)
        db.session.commit()
        flash("Your password or code was incorrect.", "error")
    return redirect(url_for("auth.profile"))


MFA_ENROLMENT_ENDPOINTS = {"auth.two_factor_setup", "auth.logout", "static", "public.healthz"}


def enforce_mfa_enrolment():
    """Users whose role requires two-factor must enrol before they can use anything else."""
    if (current_user.is_authenticated and current_user.mfa_required and not current_user.mfa_enabled
            and request.endpoint not in MFA_ENROLMENT_ENDPOINTS):
        if request.endpoint and not request.endpoint.startswith("webhooks."):
            flash("Your role requires two-factor authentication. Set it up to continue.", "warning")
            return redirect(url_for("auth.two_factor_setup"))
    return None
