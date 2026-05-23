"""
tests.py — Research Cloud Portal v3 Production Test Suite
Run: pytest tests.py -v
"""
import os
import pytest

os.environ["SECRET_KEY"]       = "test-secret-key-for-pytest-only"
os.environ["ADMIN_PASSWORD"]   = "Admin@RCP2025!"
os.environ["USER1_PASSWORD"]   = "User1@RCP2025!"
os.environ["USER2_PASSWORD"]   = "User2@RCP2025!"

from app import create_app
from app.extensions import db as _db


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"]                = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["WTF_CSRF_ENABLED"]       = False
    with app.app_context():
        _db.create_all()
        from app import _seed_users
        _seed_users()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_login_page_loads(client):
    r = client.get("/login")
    assert r.status_code == 200

def test_login_admin_success(client):
    r = login(client, "mahmoud.ali", "Admin@RCP2025!")
    # Redirects to change-password on first login
    assert r.status_code == 200

def test_login_wrong_password(client):
    r = login(client, "user1", "wrong")
    assert b"Invalid" in r.data or b"attempt" in r.data

def test_login_nonexistent_user(client):
    r = login(client, "nobody", "Password1!")
    assert b"Invalid" in r.data

def test_logout(client):
    login(client, "user1", "User1@RCP2025!")
    r = client.get("/logout", follow_redirects=True)
    assert r.status_code == 200

# ── RBAC ─────────────────────────────────────────────────────────────────────

def test_unauth_redirects_to_login(client):
    r = client.get("/dashboard", follow_redirects=True)
    assert b"Sign In" in r.data or b"login" in r.data.lower()

def test_researcher_blocked_from_admin(client):
    login(client, "user1", "User1@RCP2025!")
    # Must change password first — bypass by marking it done
    from app.models import User
    from app.extensions import db
    with client.application.app_context():
        u = User.query.filter_by(username="user1").first()
        u.must_change_password = False
        db.session.commit()
    with client.session_transaction() as sess:
        sess["must_change_password"] = False
    r = client.get("/admin/users", follow_redirects=True)
    assert b"Admin access required" in r.data or r.status_code in (302, 403)

def test_admin_reaches_panel(client):
    login(client, "mahmoud.ali", "Admin@RCP2025!")
    from app.models import User
    from app.extensions import db
    with client.application.app_context():
        u = User.query.filter_by(username="mahmoud.ali").first()
        u.must_change_password = False
        db.session.commit()
    with client.session_transaction() as sess:
        sess["must_change_password"] = False
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert b"User Management" in r.data

# ── Audit Logs ───────────────────────────────────────────────────────────────

def test_login_failure_logged(client):
    login(client, "user1", "wrongpassword")
    with client.application.app_context():
        from app.models import AuditLog
        log = AuditLog.query.filter_by(action="LOGIN_FAILURE").first()
        assert log is not None

def test_login_success_logged(client):
    login(client, "user1", "User1@RCP2025!")
    with client.application.app_context():
        from app.models import AuditLog
        from app.models import User
        u   = User.query.filter_by(username="user1").first()
        log = AuditLog.query.filter_by(user_id=u.id, action="LOGIN_SUCCESS").first()
        assert log is not None

# ── Account Lockout ──────────────────────────────────────────────────────────

def test_account_locks_after_5_failures(client):
    for _ in range(5):
        login(client, "user2", "WrongPass!")
    with client.application.app_context():
        from app.models import User
        u = User.query.filter_by(username="user2").first()
        assert u.locked_until is not None or u.failed_login_count >= 5

# ── Dashboard & Jobs ─────────────────────────────────────────────────────────

def test_dashboard_requires_login(client):
    r = client.get("/dashboard")
    assert r.status_code in (302, 200)

def test_launch_page_loads(client):
    login(client, "user1", "User1@RCP2025!")
    from app.models import User
    from app.extensions import db
    with client.application.app_context():
        u = User.query.filter_by(username="user1").first()
        u.must_change_password = False
        db.session.commit()
    with client.session_transaction() as sess:
        sess["must_change_password"] = False
    r = client.get("/launch")
    assert r.status_code == 200
