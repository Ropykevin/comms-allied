import pytest

from app import create_app
from app.cli import create_user, seed_reference_data
from app.extensions import db as _db
from app.messaging.service import message_service
from app.permissions import ROLE_ADMIN, ROLE_MARKETING, ROLE_SUPER_ADMIN, ROLE_SUPPORT, ROLE_VIEWER

PASSWORD = "Travel-Safe-2026"


@pytest.fixture(scope="session")
def _app():
    # One app per session so Jinja templates compile once; the database is reset per test.
    return create_app("testing")


@pytest.fixture()
def app(_app, tmp_path):
    _app.config["UPLOAD_FOLDER"] = str(tmp_path)
    with _app.app_context():
        _db.create_all()
        seed_reference_data()
        yield _app
        message_service.clear_overrides()
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def users(app):
    return {
        "super": create_user("super@alliedtours.co.ke", PASSWORD, "Sara Super", ROLE_SUPER_ADMIN),
        "admin": create_user("admin@alliedtours.co.ke", PASSWORD, "Adam Admin", ROLE_ADMIN),
        "marketing": create_user("marketing@alliedtours.co.ke", PASSWORD, "Mary Marketing", ROLE_MARKETING),
        "support": create_user("support@alliedtours.co.ke", PASSWORD, "Sam Support", ROLE_SUPPORT),
        "viewer": create_user("viewer@alliedtours.co.ke", PASSWORD, "Vic Viewer", ROLE_VIEWER),
    }


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


@pytest.fixture()
def login_as(client, users):
    def _login(role):
        client.post("/logout")
        resp = login(client, users[role].email)
        assert resp.status_code == 302, resp.data
        return client

    return _login
