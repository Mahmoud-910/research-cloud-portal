"""
app/__init__.py — Production app factory.
Users: mahmoud.ali (Admin), user1, user2 (Researcher).
"""
import os
import time
from datetime import datetime
from flask import Flask
from app.extensions import db, migrate, csrf


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates",
                static_folder="static", instance_relative_config=True)

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        raise RuntimeError("SECRET_KEY not set. Add it to your .env file.")

    app.config.update(
        SECRET_KEY                     = secret,
        SQLALCHEMY_DATABASE_URI        = os.environ.get(
            "DATABASE_URL",
            f"sqlite:///{os.path.join(app.instance_path, 'portal.db')}"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS = False,
        SQLALCHEMY_ENGINE_OPTIONS      = {"pool_pre_ping": True},
        WTF_CSRF_ENABLED               = True,
        WTF_CSRF_TIME_LIMIT            = 3600,
        MAX_CONTENT_LENGTH             = 16 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY        = True,
        SESSION_COOKIE_SAMESITE        = "Lax",
        SESSION_COOKIE_SECURE          = os.environ.get("SESSION_COOKIE_SECURE","False") == "True",
        PERMANENT_SESSION_LIFETIME     = 28800,
    )

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(os.path.join(app.root_path, "static", "uploads"), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, "static", "results"), exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    from app.security import apply_security_headers
    apply_security_headers(app)

    # Register Jinja2 filters
    import time as _time
    @app.template_filter("timestamp_fmt")
    def timestamp_fmt(ts):
        try:
            return datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return "—"

    @app.template_filter("action_category")
    def action_category(action: str) -> str:
        action = action.upper()
        if "SUCCESS" in action or "PASSWORD_CHANGE" in action:
            return "auth"
        if "FAIL" in action or "LOCK" in action or "DENIED" in action:
            return "failure"
        if "JOB" in action or "VM" in action:
            return "job"
        if "ADMIN" in action:
            return "admin"
        return "default"

    # Blueprints
    from app.auth.routes     import auth_bp
    from app.jobs.routes     import jobs_bp
    from app.admin.routes    import admin_bp
    from app.analysis.routes import analysis_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(analysis_bp)

    try:
        from app.octave.routes import octave_bp
        app.register_blueprint(octave_bp)
    except Exception:
        pass

    with app.app_context():
        db.create_all()
        _seed_users()

    return app


def _seed_users() -> None:
    from app.models import User

    seeds = [
        {
            "username": "mahmoud.ali",
            "full_name": "Mahmoud Ali",
            "email": "mahmoud.ali@university.edu",
            "password": os.environ.get("ADMIN_PASSWORD", "Admin@RCP2025!"),
            "role": "Admin",
        },
        {
            "username": "user1",
            "full_name": "User One",
            "email": "user1@university.edu",
            "password": os.environ.get("USER1_PASSWORD", "User1@RCP2025!"),
            "role": "Researcher",
        },
        {
            "username": "user2",
            "full_name": "User Two",
            "email": "user2@university.edu",
            "password": os.environ.get("USER2_PASSWORD", "User2@RCP2025!"),
            "role": "Researcher",
        },
    ]

    created = 0
    for s in seeds:
        if User.query.filter_by(username=s["username"]).first():
            continue
        u = User(
            username=s["username"], full_name=s["full_name"],
            email=s["email"], role=s["role"],
            is_active=True, must_change_password=True,
            created_at=time.time(), updated_at=time.time(),
        )
        u.set_password(s["password"])
        db.session.add(u)
        created += 1

    if created:
        db.session.commit()
        print(f"[INFO] Seeded {created} user(s).")
