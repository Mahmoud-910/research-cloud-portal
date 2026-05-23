"""
admin/routes.py — Admin panel: users, audit logs, VM usage.
"""
import time
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from app.extensions import db
from app.models import AuditLog, Job, User
from app.security import admin_required, audit

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/users")
@admin_required
def users():
    audit("ADMIN_VIEW_USERS", resource_type="PAGE")
    all_users = User.query.order_by(User.created_at.asc()).all()
    from sqlalchemy import func
    job_counts = dict(db.session.query(Job.user_id, func.count(Job.id)).group_by(Job.user_id).all())
    vm_counts  = dict(db.session.query(Job.user_id, func.count(Job.id))
                      .filter(Job.simulated==False).group_by(Job.user_id).all())
    return render_template("admin/users.html", users=all_users,
                           job_counts=job_counts, vm_counts=vm_counts, now=time.time)


@admin_bp.route("/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def toggle_user(uid):
    u = User.query.get_or_404(uid)
    if u.id == session["user_id"]:
        flash("Cannot disable your own account.", "danger")
        return redirect(url_for("admin.users"))
    u.is_active  = not u.is_active
    u.updated_at = time.time()
    db.session.commit()
    audit("ADMIN_TOGGLE_USER", resource_type="USER", resource_id=u.username, is_active=u.is_active)
    flash(f"User '{u.username}' {'enabled' if u.is_active else 'disabled'}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:uid>/reset", methods=["POST"])
@admin_required
def reset_user(uid):
    u = User.query.get_or_404(uid)
    u.must_change_password = True
    u.updated_at = time.time()
    db.session.commit()
    audit("ADMIN_FORCE_RESET", resource_type="USER", resource_id=u.username)
    flash(f"'{u.username}' will be prompted to change password on next login.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def delete_user(uid):
    u = User.query.get_or_404(uid)
    if u.id == session["user_id"]:
        flash("Cannot delete your own account.", "danger")
        return redirect(url_for("admin.users"))
    uname = u.username
    u.is_active = False
    u.username  = f"__del_{u.id}_{u.username}"[:64]
    u.updated_at = time.time()
    db.session.commit()
    audit("ADMIN_DELETE_USER", resource_type="USER", resource_id=uname)
    flash(f"User '{uname}' deleted.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/audit-logs")
@admin_required
def audit_logs():
    audit("ADMIN_VIEW_AUDIT_LOGS", resource_type="PAGE")
    page       = request.args.get("page", 1, type=int)
    action_f   = request.args.get("action", "")
    username_f = request.args.get("username", "")
    ip_f       = request.args.get("ip", "")

    q = AuditLog.query.order_by(AuditLog.created_at.desc())
    if action_f:   q = q.filter(AuditLog.action.ilike(f"%{action_f}%"))
    if username_f:
        u = User.query.filter_by(username=username_f).first()
        q = q.filter(AuditLog.user_id == u.id) if u else q.filter(False)
    if ip_f:       q = q.filter(AuditLog.ip_address.ilike(f"%{ip_f}%"))

    pagination = q.paginate(page=page, per_page=50, error_out=False)
    uid_set    = {log.user_id for log in pagination.items if log.user_id}
    users_map  = {u.id: u.username for u in User.query.filter(User.id.in_(uid_set)).all()} if uid_set else {}
    return render_template("admin/audit_logs.html", pagination=pagination,
                           logs=pagination.items, users_map=users_map,
                           action_filter=action_f, username_filter=username_f, ip_filter=ip_f)


@admin_bp.route("/vm-usage")
@admin_required
def vm_usage():
    audit("ADMIN_VIEW_VM_USAGE", resource_type="PAGE")
    from sqlalchemy import func
    stats = (db.session.query(
        User.username, User.full_name,
        func.count(Job.id).label("total"),
        func.sum(db.case((Job.simulated==False,1),else_=0)).label("real_vms"),
        func.sum(db.case((Job.status=="completed",1),else_=0)).label("completed"),
        func.sum(db.case((Job.status=="failed",1),else_=0)).label("failed"),
    ).outerjoin(Job, Job.user_id==User.id)
     .filter(User.is_active==True)
     .group_by(User.id)
     .order_by(func.count(Job.id).desc())
     .all())
    recent = Job.query.filter(Job.simulated==False).order_by(Job.created_at.desc()).limit(50).all()
    user_map = {u.id: u.username for u in User.query.all()}
    return render_template("admin/vm_usage.html", stats=stats, recent_jobs=recent, user_map=user_map)


@admin_bp.route("/api/users/<int:uid>/activity")
@admin_required
def user_activity_api(uid):
    u    = User.query.get_or_404(uid)
    logs = AuditLog.query.filter_by(user_id=uid).order_by(AuditLog.created_at.desc()).limit(100).all()
    return jsonify({"user": {"id": u.id, "username": u.username, "role": u.role},
                    "activity": [{"action": l.action, "resource_type": l.resource_type,
                                  "resource_id": l.resource_id, "ip": l.ip_address,
                                  "created_at": l.created_at, "detail": l.detail} for l in logs]})
