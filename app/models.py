"""
models.py — Production database models.
Users: mahmoud.ali (Admin), user1, user2 (Researcher)
Full audit trail, account lockout, must_change_password enforcement.
"""
import json
import time
from werkzeug.security import check_password_hash, generate_password_hash
from app.extensions import db


class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(64),  unique=True, nullable=False, index=True)
    email         = db.Column(db.String(255), unique=True, nullable=True)
    full_name     = db.Column(db.String(128), nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    role          = db.Column(db.String(32),  nullable=False, default="Researcher")

    is_active            = db.Column(db.Boolean, default=True,  nullable=False)
    must_change_password = db.Column(db.Boolean, default=True,  nullable=False)

    last_login_at      = db.Column(db.Float,      nullable=True)
    last_login_ip      = db.Column(db.String(64), nullable=True)
    failed_login_count = db.Column(db.Integer,    default=0, nullable=False)
    locked_until       = db.Column(db.Float,      nullable=True)

    created_at = db.Column(db.Float, default=time.time, nullable=False)
    updated_at = db.Column(db.Float, default=time.time, nullable=False)

    jobs       = db.relationship("Job",      backref="owner", lazy="dynamic")
    audit_logs = db.relationship("AuditLog", backref="actor", lazy="dynamic",
                                 foreign_keys="AuditLog.user_id")

    def set_password(self, plain: str) -> None:
        self.password_hash = generate_password_hash(plain, method="scrypt")
        self.updated_at    = time.time()

    def check_password(self, plain: str) -> bool:
        return check_password_hash(self.password_hash, plain)

    def is_locked(self) -> bool:
        if not self.locked_until:
            return False
        if time.time() < self.locked_until:
            return True
        self.locked_until       = None
        self.failed_login_count = 0
        db.session.commit()
        return False

    def record_failed_login(self) -> None:
        self.failed_login_count = (self.failed_login_count or 0) + 1
        if self.failed_login_count >= 5:
            self.locked_until = time.time() + 900
        self.updated_at = time.time()

    def record_successful_login(self, ip: str) -> None:
        self.failed_login_count = 0
        self.locked_until       = None
        self.last_login_at      = time.time()
        self.last_login_ip      = ip
        self.updated_at         = time.time()

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role})>"


class Job(db.Model):
    __tablename__ = "jobs"

    id      = db.Column(db.Integer, primary_key=True)
    job_id  = db.Column(db.String(16), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    vm_id     = db.Column(db.String(32), nullable=True)
    vm_ip     = db.Column(db.String(64), nullable=True)
    simulated = db.Column(db.Boolean,   default=False, nullable=False)

    celery_task_id = db.Column(db.String(64), nullable=True, index=True)
    status         = db.Column(db.String(32), default="PENDING", nullable=False, index=True)
    error_msg      = db.Column(db.Text,       nullable=True)

    created_at = db.Column(db.Float, default=time.time, nullable=False)
    updated_at = db.Column(db.Float, default=time.time, nullable=False)

    os_image = db.Column(db.String(32), nullable=True)
    os_label = db.Column(db.String(64), nullable=True)
    os_icon  = db.Column(db.String(8),  nullable=True)
    cpu      = db.Column(db.String(8),  nullable=True)
    ram      = db.Column(db.String(8),  nullable=True)
    gpu      = db.Column(db.String(32), nullable=True)

    software  = db.Column(db.String(32), nullable=True)
    sw_label  = db.Column(db.String(64), nullable=True)
    task_type = db.Column(db.String(32), nullable=True)

    dataset     = db.Column(db.String(256), nullable=True)
    result_file = db.Column(db.String(256), nullable=True)
    result_path = db.Column(db.String(512), nullable=True)

    def __repr__(self) -> str:
        return f"<Job {self.job_id} {self.status}>"


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    action        = db.Column(db.String(64),  nullable=False, index=True)
    resource_type = db.Column(db.String(32),  nullable=True)
    resource_id   = db.Column(db.String(64),  nullable=True)
    ip_address    = db.Column(db.String(64),  nullable=True)
    user_agent    = db.Column(db.String(512), nullable=True)
    detail        = db.Column(db.Text,        nullable=True)
    created_at    = db.Column(db.Float, default=time.time, nullable=False, index=True)

    @classmethod
    def write(cls, action: str, user_id=None, resource_type=None,
              resource_id=None, ip=None, ua=None, **detail_kw):
        entry = cls(
            user_id       = user_id,
            action        = action,
            resource_type = resource_type,
            resource_id   = str(resource_id) if resource_id is not None else None,
            ip_address    = ip,
            user_agent    = (ua or "")[:500],
            detail        = json.dumps(detail_kw) if detail_kw else None,
        )
        db.session.add(entry)
        return entry

    def __repr__(self) -> str:
        return f"<AuditLog {self.action}>"
