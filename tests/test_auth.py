from app.auth.routes import generate_reset_token
from app.models import AuditLog, User
from tests.conftest import PASSWORD, login


def test_password_is_hashed(users):
    user = users["admin"]
    assert user.password_hash != PASSWORD
    assert PASSWORD not in user.password_hash
    assert user.check_password(PASSWORD)
    assert not user.check_password("wrong-password")


def test_login_success_and_audit(client, users):
    resp = login(client, "admin@alliedtours.co.ke")
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["Location"]
    assert AuditLog.query.filter_by(action="user.logged_in", user_id=users["admin"].id).count() == 1
    dash = client.get("/dashboard/")
    assert dash.status_code == 200
    assert b"Total Clients" in dash.data


def test_login_failure(client, users):
    resp = login(client, "admin@alliedtours.co.ke", "nope-nope-nope")
    assert resp.status_code == 200
    assert b"Invalid email or password" in resp.data


def test_inactive_user_cannot_login(client, users, db):
    users["viewer"].is_active_user = False
    db.session.commit()
    resp = login(client, "viewer@alliedtours.co.ke")
    assert resp.status_code == 200
    assert b"deactivated" in resp.data


def test_logout(client, users):
    login(client, "admin@alliedtours.co.ke")
    resp = client.post("/logout")
    assert resp.status_code == 302
    assert client.get("/dashboard/").status_code == 302


def test_login_required_redirects(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_open_redirect_blocked(client, users):
    resp = client.post("/login?next=https://evil.example/x", data={"email": "admin@alliedtours.co.ke", "password": PASSWORD})
    assert resp.headers["Location"].endswith("/dashboard/")


def test_password_reset_flow(client, users, db):
    user = users["support"]
    resp = client.post("/forgot-password", data={"email": user.email})
    assert resp.status_code == 302
    token = generate_reset_token(user)
    resp = client.post(f"/reset-password/{token}", data={"password": "Brand-New-Pass-1", "confirm": "Brand-New-Pass-1"})
    assert resp.status_code == 302
    db.session.refresh(user)
    assert user.check_password("Brand-New-Pass-1")
    # Token is single-use
    resp = client.get(f"/reset-password/{token}")
    assert resp.status_code == 302
    assert "forgot-password" in resp.headers["Location"]


def test_change_password_requires_current(client, users, db):
    login(client, "viewer@alliedtours.co.ke")
    client.post("/change-password", data={"current_password": "wrong", "password": "Another-Pass-22", "confirm": "Another-Pass-22"})
    assert db.session.get(User, users["viewer"].id).check_password(PASSWORD)
    client.post("/change-password", data={"current_password": PASSWORD, "password": "Another-Pass-22", "confirm": "Another-Pass-22"})
    assert db.session.get(User, users["viewer"].id).check_password("Another-Pass-22")


def test_weak_password_rejected(client, users, db):
    login(client, "viewer@alliedtours.co.ke")
    client.post("/change-password", data={"current_password": PASSWORD, "password": "short", "confirm": "short"})
    assert db.session.get(User, users["viewer"].id).check_password(PASSWORD)


def test_role_authorization_enforced_on_backend(login_as):
    c = login_as("support")
    assert c.get("/settings/users").status_code == 403
    assert c.get("/settings/audit-log").status_code == 403
    c = login_as("admin")
    assert c.get("/settings/users").status_code == 403  # only super admin manages users
    assert c.get("/settings/audit-log").status_code == 200
    c = login_as("super")
    assert c.get("/settings/users").status_code == 200


def test_super_admin_creates_user(login_as):
    c = login_as("super")
    from app.models import Role
    role = Role.query.filter_by(name="viewer").first()
    resp = c.post("/settings/users/new", data={
        "full_name": "New Person", "email": "new@alliedtours.co.ke", "role_id": role.id,
        "is_active": "y", "password": "Welcome-To-Allied-1",
    })
    assert resp.status_code == 302
    assert User.query.filter_by(email="new@alliedtours.co.ke").first().role.name == "viewer"


def test_cannot_demote_last_super_admin(login_as, users):
    c = login_as("super")
    from app.models import Role
    viewer = Role.query.filter_by(name="viewer").first()
    sup = users["super"]
    c.post(f"/settings/users/{sup.id}/edit", data={
        "full_name": sup.full_name, "email": sup.email, "role_id": viewer.id, "is_active": "y",
    })
    assert User.query.filter_by(id=sup.id).one().role.name == "super_admin"


def test_security_headers(client):
    resp = client.get("/login")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]


def test_healthz(client):
    assert client.get("/healthz").json["status"] == "ok"
