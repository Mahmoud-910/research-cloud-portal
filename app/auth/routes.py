"""
auth/routes.py — Login, logout, password change with full audit trail.
"""
import time
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from app.extensions import db
from app.models import AuditLog, User
from app.security import audit, get_client_ip, rate_limit, validate_password_strength

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
@rate_limit(limit=5, window=60)
def login():
    if "user_id" in session:
        return redirect(url_for("jobs.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        ip       = get_client_ip()

        user = User.query.filter_by(username=username).first()

        if user is None:
            AuditLog.write(action="LOGIN_FAILURE", ip=ip,
                           ua=request.user_agent.string,
                           reason="unknown_username", attempted_username=username)
            db.session.commit()
            flash("Invalid username or password.", "danger")
            return render_template("auth/login.html")

        if not user.is_active:
            AuditLog.write(action="LOGIN_FAILURE", user_id=user.id, ip=ip,
                           ua=request.user_agent.string, reason="account_disabled")
            db.session.commit()
            flash("Your account has been disabled. Contact the administrator.", "danger")
            return render_template("auth/login.html")

        if user.is_locked():
            remaining = max(0, int(user.locked_until - time.time()))
            AuditLog.write(action="LOGIN_FAILURE", user_id=user.id, ip=ip,
                           ua=request.user_agent.string, reason="account_locked")
            db.session.commit()
            flash(f"Account locked. Try again in {remaining // 60}m {remaining % 60}s.", "danger")
            return render_template("auth/login.html")

        if not user.check_password(password):
            user.record_failed_login()
            action = "ACCOUNT_LOCKED" if user.locked_until else "LOGIN_FAILURE"
            AuditLog.write(action=action, user_id=user.id, ip=ip,
                           ua=request.user_agent.string, reason="wrong_password",
                           failed_count=user.failed_login_count)
            db.session.commit()
            remaining_attempts = max(0, 5 - user.failed_login_count)
            if user.locked_until:
                flash("Too many failed attempts — account locked for 15 minutes.", "danger")
            else:
                flash(f"Invalid username or password. {remaining_attempts} attempt(s) remaining.", "danger")
            return render_template("auth/login.html")

        # Success
        user.record_successful_login(ip)
        AuditLog.write(action="LOGIN_SUCCESS", user_id=user.id, ip=ip,
                       ua=request.user_agent.string)
        db.session.commit()

        session.clear()
        session.permanent = True
        session["user_id"]              = user.id
        session["username"]             = user.username
        session["role"]                 = user.role
        session["name"]                 = user.full_name
        session["must_change_password"] = user.must_change_password

        if user.must_change_password:
            flash("Welcome! Please set a new password before continuing.", "warning")
            return redirect(url_for("auth.change_password"))

        return redirect(request.args.get("next") or url_for("jobs.dashboard"))

    return render_template("auth/login.html")


@auth_bp.route("/logout")
def logout():
    if "user_id" in session:
        AuditLog.write(action="LOGOUT", user_id=session["user_id"],
                       ip=get_client_ip(), ua=request.user_agent.string)
        db.session.commit()
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        current  = request.form.get("current_password", "")
        new_pw   = request.form.get("new_password", "")
        confirm  = request.form.get("confirm_password", "")
        user     = User.query.get(session["user_id"])

        if not user.check_password(current):
            flash("Current password is incorrect.", "danger")
            return render_template("auth/change_password.html")
        if new_pw != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("auth/change_password.html")

        errors = validate_password_strength(new_pw, username=user.username)
        for e in errors:
            flash(e, "danger")
        if errors:
            return render_template("auth/change_password.html")

        user.set_password(new_pw)
        user.must_change_password = False
        AuditLog.write(action="PASSWORD_CHANGE", user_id=user.id,
                       ip=get_client_ip(), ua=request.user_agent.string)
        db.session.commit()
        session["must_change_password"] = False
        flash("Password changed successfully.", "success")
        return redirect(url_for("jobs.dashboard"))

    return render_template("auth/change_password.html")
