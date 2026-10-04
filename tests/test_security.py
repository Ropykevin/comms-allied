import os
import time
from datetime import timedelta
from types import SimpleNamespace

import pyotp
import pytest

from app import _check_production_config
from app.auth.routes import _safe_next
from app.models import AuditLog, Client, Role, User, WebhookEvent
from app.permissions import ROLE_ADMIN
from app.utils import utcnow
from tests.conftest import PASSWORD, login


def _enable_mfa(db, user):
    user.totp_secret = pyotp.random_base32()
    user.mfa_enabled = True
    db.session.commit()
    return pyotp.TOTP(user.totp_secret)


# ---- open redirect ----------------------------------------------------------------

@pytest.mark.parametrize("target", [
    "https://evil.example", "//evil.example", "/\\evil.example", "\\\\evil.example", "/\tevil",
    "javascript:alert(1)", "", None,
])
def test_safe_next_rejects_external_targets(app, target):
    assert _safe_next(target) is None


def test_safe_next_allows_local_paths(app):
    assert _safe_next("/clients/?q=kamau") == "/clients/?q=kamau"


def test_backslash_next_not_followed(client, users):
    resp = client.post("/login?next=/%5Cevil.example", data={"email": users["admin"].email, "password": PASSWORD})
    assert resp.headers["Location"].endswith("/dashboard/")


# ---- login auditing ---------------------------------------------------------------

def test_failed_logins_are_audited(client, users):
    login(client, users["admin"].email, "wrong-password-123")
    login(client, "nobody@example.com", "wrong-password-123")
    failures = AuditLog.query.filter_by(action="user.login_failed").all()
    assert len(failures) == 2
    assert {f.details["email"] for f in failures} == {users["admin"].email, "nobody@example.com"}
    assert {f.entity_id for f in failures} == {users["admin"].id, None}


# ---- sessions ---------------------------------------------------------------------

def test_password_change_signs_out_other_sessions(app, users):
    phone, laptop = app.test_client(), app.test_client()
    login(phone, users["viewer"].email)
    login(laptop, users["viewer"].email)
    assert phone.get("/dashboard/").status_code == 200
    laptop.post("/change-password", data={"current_password": PASSWORD, "password": "Another-Pass-22",
                                          "confirm": "Another-Pass-22"})
    assert phone.get("/dashboard/").status_code == 302  # stolen/old session no longer works
    assert laptop.get("/dashboard/").status_code == 200  # the device that changed it stays signed in


def test_deactivation_and_role_change_revoke_sessions(app, login_as, users, db):
    victim = app.test_client()
    login(victim, users["marketing"].email)
    assert victim.get("/dashboard/").status_code == 200
    admin = login_as("super")
    viewer = Role.query.filter_by(name="viewer").first()
    m = users["marketing"]
    admin.post(f"/settings/users/{m.id}/edit", data={
        "full_name": m.full_name, "email": m.email, "role_id": viewer.id, "is_active": "y",
    })
    assert victim.get("/dashboard/").status_code == 302


def test_super_admin_editing_self_stays_signed_in(login_as, users):
    c = login_as("super")
    s = users["super"]
    resp = c.post(f"/settings/users/{s.id}/edit", data={
        "full_name": s.full_name, "email": "sara@alliedtours.co.ke", "role_id": s.role_id, "is_active": "y",
    })
    assert resp.status_code == 302
    assert c.get("/dashboard/").status_code == 200


def test_legacy_session_id_format_rejected(app, users):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(users["admin"].id)  # pre-versioning cookie
        sess["_fresh"] = True
    assert c.get("/dashboard/").status_code == 302


# ---- two-factor -------------------------------------------------------------------

def test_login_with_two_factor(client, users, db):
    totp = _enable_mfa(db, users["admin"])
    resp = login(client, users["admin"].email)
    assert resp.headers["Location"].endswith("/login/two-factor")
    assert client.get("/dashboard/").status_code == 302  # password alone is not enough

    resp = client.post("/login/two-factor", data={"code": "000000"})
    assert b"incorrect" in resp.data
    assert AuditLog.query.filter_by(action="user.mfa_failed").count() == 1

    resp = client.post("/login/two-factor", data={"code": totp.now()})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard/")
    assert client.get("/dashboard/").status_code == 200


def test_two_factor_code_cannot_be_replayed(app, users, db):
    totp = _enable_mfa(db, users["admin"])
    code = totp.now()
    first, second = app.test_client(), app.test_client()
    login(first, users["admin"].email)
    first.post("/login/two-factor", data={"code": code})
    assert first.get("/dashboard/").status_code == 200
    login(second, users["admin"].email)
    second.post("/login/two-factor", data={"code": code})
    assert second.get("/dashboard/").status_code == 302


def test_two_factor_attempts_are_limited(client, users, db):
    _enable_mfa(db, users["admin"])
    login(client, users["admin"].email)
    for _ in range(5):
        resp = client.post("/login/two-factor", data={"code": "000000"})
    assert resp.headers["Location"].endswith("/login")
    assert client.get("/login/two-factor").headers["Location"].endswith("/login")


def test_two_factor_pending_state_expires(client, users, db):
    totp = _enable_mfa(db, users["admin"])
    login(client, users["admin"].email)
    with client.session_transaction() as sess:
        pending = sess["mfa_pending"]
        pending["at"] = int(time.time()) - 600
        sess["mfa_pending"] = pending
    resp = client.post("/login/two-factor", data={"code": totp.now()})
    assert resp.headers["Location"].endswith("/login")
    assert client.get("/dashboard/").status_code == 302


def test_required_roles_must_enrol(app, client, users, db, monkeypatch):
    monkeypatch.setitem(app.config, "MFA_REQUIRED_ROLES", (ROLE_ADMIN,))
    login(client, users["admin"].email)
    resp = client.get("/clients/")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/profile/two-factor")

    assert client.get("/profile/two-factor").status_code == 200
    with client.session_transaction() as sess:
        secret = sess["mfa_setup_secret"]
    client.post("/profile/two-factor", data={"code": "123456" if pyotp.TOTP(secret).now() != "123456" else "654321"})
    assert not db.session.get(User, users["admin"].id).mfa_enabled

    resp = client.post("/profile/two-factor", data={"code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 302
    user = db.session.get(User, users["admin"].id)
    assert user.mfa_enabled and user.totp_secret == secret
    assert client.get("/clients/").status_code == 200
    # Required roles cannot switch it off themselves.
    assert client.post("/profile/two-factor/disable", data={"password": PASSWORD, "code": "000000"}).status_code == 403


def test_roles_not_required_are_not_forced(app, client, users, monkeypatch):
    monkeypatch.setitem(app.config, "MFA_REQUIRED_ROLES", (ROLE_ADMIN,))
    login(client, users["viewer"].email)
    assert client.get("/clients/").status_code == 200


def test_optional_two_factor_can_be_disabled_with_password_and_code(app, client, users, db):
    user = users["viewer"]
    totp = _enable_mfa(db, user)
    login(client, user.email)
    client.post("/login/two-factor", data={"code": totp.now()})
    client.post("/profile/two-factor/disable", data={"password": "wrong-password-1", "code": totp.now()})
    assert db.session.get(User, user.id).mfa_enabled
    user.totp_last_step = None
    db.session.commit()
    client.post("/profile/two-factor/disable", data={"password": PASSWORD, "code": totp.now()})
    assert not db.session.get(User, user.id).mfa_enabled


def test_admin_can_reset_lost_two_factor(app, login_as, users, db):
    victim = users["admin"]
    _enable_mfa(db, victim)
    c = login_as("super")
    c.post(f"/settings/users/{victim.id}/edit", data={
        "full_name": victim.full_name, "email": victim.email, "role_id": victim.role_id, "is_active": "y",
        "reset_mfa": "y",
    })
    user = db.session.get(User, victim.id)
    assert not user.mfa_enabled and user.totp_secret is None
    assert AuditLog.query.filter_by(action="user.updated", entity_id=victim.id).one().details["mfa_reset_by_admin"]


def test_cli_reset_mfa_and_password_policy(app, users, db):
    _enable_mfa(db, users["super"])
    runner = app.test_cli_runner()
    result = runner.invoke(args=["reset-mfa", "--email", users["super"].email])
    assert result.exit_code == 0, result.output
    assert not db.session.get(User, users["super"].id).mfa_enabled

    result = runner.invoke(args=["create-user", "--email", "weak@alliedtours.co.ke", "--name", "Weak",
                                 "--role", "viewer", "--password", "password123"])
    assert result.exit_code != 0
    assert User.query.filter_by(email="weak@alliedtours.co.ke").first() is None


# ---- input limits -----------------------------------------------------------------

def test_import_rejects_over_long_fields(app):
    from app.clients.importer import analyze
    token = "a" * 32
    folder = os.path.join(app.config["UPLOAD_FOLDER"], "imports")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{token}.csv"), "w", encoding="utf-8") as fh:
        fh.write(f"Name,Phone,Company\n{'N' * 151},+254712345678,ACME\nOk Name,+254712345679,{'C' * 151}\n")
    summary = analyze(token)
    assert summary["valid"] == 0 and summary["invalid"] == 2
    assert "longer than 150" in summary["rows"][0]["reason"]


def test_inbound_with_huge_subject_and_name_is_stored(app, db):
    from app.messaging.base import InboundMessage
    from app.messaging.service import message_service
    msg = message_service.handle_inbound(InboundMessage(
        channel="email", sender="stranger@example.com", content="Hello", provider_message_id="m-1",
        subject="S" * 1000, sender_name="X" * 500,
    ))
    db.session.commit()
    assert len(msg.subject) == 255
    assert len(Client.query.filter_by(email="stranger@example.com").one().full_name) == 150


# ---- retention --------------------------------------------------------------------

def test_purge_old_data(app, db):
    from app.retention import purge_old_data
    folder = os.path.join(app.config["UPLOAD_FOLDER"], "imports")
    os.makedirs(folder, exist_ok=True)
    old, fresh, report = (os.path.join(folder, n) for n in ("a" * 32 + ".csv", "b" * 32 + ".csv", "c" * 32 + "-errors.csv"))
    for path in (old, fresh, report):
        with open(path, "w") as fh:
            fh.write("x")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    os.utime(report, (two_days_ago, two_days_ago))  # error reports are kept for a week

    db.session.add_all([
        WebhookEvent(channel="sms", provider="at", event_key="old", payload={"phone": "+254712345678"},
                     received_at=utcnow() - timedelta(days=40)),
        WebhookEvent(channel="sms", provider="at", event_key="new", payload={"phone": "+254712345678"}),
    ])
    db.session.commit()

    result = purge_old_data()
    assert result["import_files"] == 1 and result["webhook_payloads"] == 1
    assert not os.path.exists(old) and os.path.exists(fresh) and os.path.exists(report)
    db.session.expire_all()
    assert WebhookEvent.query.filter_by(event_key="old").one().payload is None
    assert WebhookEvent.query.filter_by(event_key="new").one().payload
    assert purge_old_data()["webhook_payloads"] == 0


# ---- configuration ----------------------------------------------------------------

@pytest.mark.parametrize("key", ["dev-insecure-change-me", "replace-with-a-long-random-string", "short-key"])
def test_production_rejects_weak_secret_key(key):
    with pytest.raises(RuntimeError):
        _check_production_config(SimpleNamespace(config={"SECRET_KEY": key, "DEBUG": False}))


def test_production_rejects_debug():
    with pytest.raises(RuntimeError):
        _check_production_config(SimpleNamespace(config={"SECRET_KEY": "k" * 48, "DEBUG": True}))
    _check_production_config(SimpleNamespace(config={"SECRET_KEY": "k" * 48, "DEBUG": False}))
