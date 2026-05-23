"""
security.py — Auth decorators, rate limiting, audit helper, security headers.
"""
import functools
import re
import time
from collections import defaultdict
from threading import Lock
from typing import Callable

from flask import abort, flash, jsonify, redirect, request, session, url_for, current_app

_rate_store: dict = defaultdict(list)
_rate_lock = Lock()


def get_client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def audit(action: str, resource_type: str = None, resource_id=None, **detail):
    try:
        from app.extensions import db
        from app.models import AuditLog
        AuditLog.write(
            action=action, user_id=session.get("user_id"),
            resource_type=resource_type, resource_id=resource_id,
            ip=get_client_ip(),
            ua=request.user_agent.string if request.user_agent else None,
            **detail,
        )
        db.session.commit()
    except Exception as exc:
        current_app.logger.error("Audit write failed: %s", exc)


def login_required(f: Callable) -> Callable:
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        if session.get("must_change_password") and \
                request.endpoint not in ("auth.change_password", "auth.logout"):
            flash("You must change your password before continuing.", "warning")
            return redirect(url_for("auth.change_password"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f: Callable) -> Callable:
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        if session.get("role") != "Admin":
            audit("ADMIN_ACCESS_DENIED", resource_type="ROUTE", resource_id=request.path)
            if request.is_json:
                return jsonify({"error": "Admin access required"}), 403
            flash("Admin access required.", "danger")
            return redirect(url_for("jobs.dashboard"))
        return f(*args, **kwargs)
    return decorated


def _check_rate(key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    with _rate_lock:
        _rate_store[key] = [t for t in _rate_store[key] if now - t < window]
        if len(_rate_store[key]) >= limit:
            return False
        _rate_store[key].append(now)
        return True


def rate_limit(limit: int = 10, window: int = 60, key_func: Callable = None):
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            bucket = key_func() if key_func else f"{request.endpoint}:{get_client_ip()}"
            if not _check_rate(bucket, limit, window):
                flash("Too many requests. Please wait before trying again.", "warning")
                return redirect(request.referrer or url_for("auth.login"))
            return f(*args, **kwargs)
        return decorated
    return decorator


def apply_security_headers(app) -> None:
    @app.after_request
    def set_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]     = "camera=(), microphone=(), geolocation=()"
        return response


SPECIAL_CHARS = r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?]"


def validate_password_strength(password: str, username: str = "") -> list:
    errors = []
    if len(password) < 12:
        errors.append("Password must be at least 12 characters long.")
    if not re.search(r"[A-Z]", password):
        errors.append("Must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Must contain at least one lowercase letter.")
    if not re.search(r"\d", password):
        errors.append("Must contain at least one digit.")
    if not re.search(SPECIAL_CHARS, password):
        errors.append("Must contain at least one special character (!@#$%^ etc.).")
    if username and username.lower() in password.lower():
        errors.append("Password must not contain your username.")
    return errors
