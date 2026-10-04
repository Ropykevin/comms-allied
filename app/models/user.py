from flask import current_app
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, login_manager
from app.permissions import ROLE_PERMISSIONS
from app.utils import utcnow


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    label = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    users = db.relationship("User", back_populates="role")

    @property
    def permissions(self):
        return ROLE_PERMISSIONS.get(self.name, set())

    def __repr__(self):
        return f"<Role {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(32))
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)
    is_active_user = db.Column("is_active", db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime)
    # Bumped whenever credentials change; every existing session and "remember me" cookie then stops working.
    session_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    totp_secret = db.Column(db.String(64))
    # Last accepted TOTP time-step, so a code can't be replayed within its validity window.
    totp_last_step = db.Column(db.BigInteger)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    role = db.relationship("Role", back_populates="users")

    def set_password(self, password):
        method = current_app.config.get("PASSWORD_HASH_METHOD", "scrypt")
        self.password_hash = generate_password_hash(password, method=method)
        self.password_changed_at = utcnow()
        self.revoke_sessions()

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def revoke_sessions(self):
        self.session_version = (self.session_version or 1) + 1

    def get_id(self):
        return f"{self.id}:{self.session_version or 1}"

    @property
    def is_active(self):
        return bool(self.is_active_user)

    @property
    def mfa_required(self):
        return self.role is not None and self.role.name in current_app.config.get("MFA_REQUIRED_ROLES", ())

    def can(self, permission):
        return self.is_active and self.role is not None and permission in self.role.permissions

    def has_role(self, *names):
        return self.role is not None and self.role.name in names

    @property
    def first_name(self):
        return (self.full_name or "").split(" ")[0]

    @property
    def initials(self):
        parts = [p for p in (self.full_name or "").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    def __repr__(self):
        return f"<User {self.email}>"


@login_manager.user_loader
def load_user(user_id):
    """Session ids are "<id>:<session_version>"; a stale version means the session was revoked."""
    try:
        uid, version = str(user_id).split(":", 1)
        user = db.session.get(User, int(uid))
        if user is None or int(version) != (user.session_version or 1):
            return None
        return user
    except (TypeError, ValueError):
        return None
