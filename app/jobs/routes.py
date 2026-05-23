"""
jobs/routes.py — Dashboard, unified VM launch, status polling, results, download.
"""
import csv
import json
import os
import random
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Blueprint, abort, flash, jsonify, redirect,
                   render_template, request, send_from_directory, session, url_for)
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import AuditLog, Job
from app.security import audit, login_required, rate_limit

jobs_bp = Blueprint("jobs", __name__)

OS_TEMPLATES = {
    "ubuntu-22":   {"name": "Ubuntu 22.04 LTS",   "template_id": 14, "icon": "🐧"},
    "ubuntu-20":   {"name": "Ubuntu 20.04 LTS",   "template_id": 10, "icon": "🐧"},
    "centos-9":    {"name": "CentOS Stream 9",     "template_id": 11, "icon": "🎩"},
    "windows-10":  {"name": "Windows 10 Pro",      "template_id": 12, "icon": "🪟"},
    "windows-srv": {"name": "Windows Server 2022", "template_id": 13, "icon": "🪟"},
}

SOFTWARE_STACKS = {
    "python-ds": {"name": "Python / Data Science",      "packages": "numpy, pandas, scikit-learn, matplotlib"},
    "matlab":    {"name": "MATLAB / Octave",            "packages": "GNU Octave, statistics, io"},
    "julia":     {"name": "Julia Scientific Computing", "packages": "Julia 1.10, CSV, DataFrames"},
    "r-stats":   {"name": "R / Statistical Computing",  "packages": "R 4.3, tidyverse, ggplot2"},
    "mapreduce": {"name": "Hadoop / MapReduce",         "packages": "Hadoop 3.3, Spark 3.5"},
}

TASK_TYPE_MAP = {
    "python-ds": "python_ds",
    "matlab":    "octave",
    "julia":     "julia",
    "r-stats":   "r",
    "mapreduce": None,
}

STEP_LABELS = {
    "provisioning":  ("Provisioning VM",       10),
    "resizing_disk": ("Resizing disk",         20),
    "booting":       ("VM booting",            35),
    "waiting_ssh":   ("Waiting for SSH",       50),
    "bootstrapping": ("Installing libraries",  65),
    "uploading":     ("Uploading dataset",     78),
    "running":       ("Running analysis",      88),
    "completed":     ("Completed",            100),
    "failed":        ("Failed",                 0),
    "terminated":    ("Terminated",             0),
    "PENDING":       ("Pending",                5),
    "BOOT":          ("Booting",               30),
    "RUNNING":       ("Running",               60),
    "DONE":          ("Done",                 100),
}


def _results_dir() -> str:
    from flask import current_app
    return os.path.join(current_app.root_path, "static", "results")


def _upload_dir() -> str:
    from flask import current_app
    return os.path.join(current_app.root_path, "static", "uploads")


def advance_job_status(job: Job) -> None:
    if job.simulated and job.status not in ("DONE", "TERMINATED", "completed", "failed"):
        elapsed = time.time() - job.created_at
        if elapsed < 10:   job.status = "PENDING"
        elif elapsed < 30: job.status = "BOOT"
        elif elapsed < 90: job.status = "RUNNING"
        elif elapsed < 110:
            job.status = "DONE"
            if not job.result_file:
                job.result_file = _make_dummy_result(job)
        else: job.status = "TERMINATED"
        db.session.commit()


def _make_dummy_result(job: Job) -> str:
    folder = _results_dir()
    os.makedirs(folder, exist_ok=True)
    fname = f"result_{job.job_id}.csv"
    path  = os.path.join(folder, fname)
    rows  = [["epoch", "metric", "value", "timestamp"]]
    for epoch in range(1, 21):
        for metric in ["accuracy", "loss", "f1_score"]:
            val = round(max(0.01, 1.0 - epoch/25 + random.uniform(-0.05,0.05)),4) \
                  if metric=="loss" \
                  else round(min(0.99, 0.5 + epoch/28 + random.uniform(-0.03,0.03)),4)
            rows.append([epoch, metric, val, datetime.utcnow().isoformat()])
    with open(path,"w",newline="") as f:
        csv.writer(f).writerows(rows)
    return fname


# ── Routes ────────────────────────────────────────────────────────────────────

@jobs_bp.route("/dashboard")
@login_required
def dashboard():
    is_admin = session["role"] == "Admin"
    jobs = Job.query.order_by(Job.created_at.desc()).all() if is_admin \
           else Job.query.filter_by(user_id=session["user_id"]).order_by(Job.created_at.desc()).all()
    for job in jobs:
        advance_job_status(job)
    return render_template("jobs/dashboard.html", jobs=jobs,
                           os_templates=OS_TEMPLATES, software_stacks=SOFTWARE_STACKS,
                           step_labels=STEP_LABELS, is_admin=is_admin)


@jobs_bp.route("/launch", methods=["GET","POST"])
@login_required
@rate_limit(limit=10, window=3600, key_func=lambda: f"launch:{session.get('user_id')}")
def launch():
    if request.method == "POST":
        os_image  = request.form.get("os_image", "ubuntu-22")
        cpu       = int(request.form.get("cpu", 2))
        ram       = int(request.form.get("ram", 4))
        gpu_raw   = request.form.get("gpu", "none")
        software  = request.form.get("software", "python-ds")
        keep_vm   = request.form.get("keep_vm") == "1"
        dataset   = request.files.get("dataset")

        task_type = TASK_TYPE_MAP.get(software)
        if task_type is None:
            flash(f"'{SOFTWARE_STACKS.get(software,{}).get('name',software)}' not yet supported in the unified pipeline.", "warning")
            return redirect(url_for("jobs.launch"))

        if not dataset or not dataset.filename:
            flash("Please upload a CSV dataset.", "danger")
            return redirect(url_for("jobs.launch"))

        if not ("." in dataset.filename and dataset.filename.rsplit(".",1)[1].lower() in {"csv","txt"}):
            flash("Only CSV / TXT files are accepted.", "danger")
            return redirect(url_for("jobs.launch"))

        upload_folder = _upload_dir()
        os.makedirs(upload_folder, exist_ok=True)
        safe_name   = secure_filename(f"{session['username']}_{int(time.time())}_{dataset.filename}")
        dataset_abs = os.path.join(upload_folder, safe_name)
        dataset.save(dataset_abs)

        job_id = str(uuid.uuid4())[:8].upper()
        job = Job(
            job_id=job_id, user_id=session["user_id"],
            simulated=False, status="provisioning",
            created_at=time.time(), updated_at=time.time(),
            os_image=os_image,
            os_label=OS_TEMPLATES.get(os_image,{}).get("name",os_image),
            os_icon=OS_TEMPLATES.get(os_image,{}).get("icon","🖥️"),
            cpu=str(cpu), ram=str(ram), gpu=gpu_raw,
            software=software,
            sw_label=SOFTWARE_STACKS.get(software,{}).get("name",software),
            task_type=task_type, dataset=safe_name,
        )
        db.session.add(job)
        db.session.commit()

        try:
            from tasks_vm_unified import run_unified_pipeline
            t = run_unified_pipeline.apply_async(kwargs={
                "job_id": job_id, "dataset_path": dataset_abs,
                "task_type": task_type, "cpu": cpu,
                "ram_gb": ram, "gpu": gpu_raw != "none", "keep_vm": keep_vm,
            })
            job.celery_task_id = t.id
            db.session.commit()
        except Exception as e:
            job.status    = "failed"
            job.error_msg = str(e)
            db.session.commit()
            flash(f"Failed to start pipeline: {e}", "danger")
            return redirect(url_for("jobs.dashboard"))

        AuditLog.write(action="JOB_LAUNCH", user_id=session["user_id"],
                       resource_type="JOB", resource_id=job_id,
                       ip=request.remote_addr, task_type=task_type,
                       cpu=cpu, ram=ram)
        db.session.commit()

        flash(f"Job #{job_id} launched! Pipeline running in background.", "success")
        return redirect(url_for("jobs.job_progress", job_id=job_id))

    return render_template("jobs/launch.html",
                           os_templates=OS_TEMPLATES, software_stacks=SOFTWARE_STACKS)


@jobs_bp.route("/request", methods=["GET","POST"])
@login_required
def request_vm():
    return redirect(url_for("jobs.launch"), 301)


@jobs_bp.route("/progress/<job_id>")
@login_required
def job_progress(job_id):
    job = Job.query.filter_by(job_id=job_id).first_or_404()
    if session["role"] != "Admin" and job.user_id != session["user_id"]:
        abort(403)
    return render_template("jobs/progress.html", job=job, step_labels=STEP_LABELS)


@jobs_bp.route("/status/<job_id>")
@login_required
def job_status(job_id):
    job = Job.query.filter_by(job_id=job_id).first()
    if not job:
        return jsonify({"error": "Not found"}), 404
    if session["role"] != "Admin" and job.user_id != session["user_id"]:
        return jsonify({"error": "Forbidden"}), 403
    if job.simulated:
        advance_job_status(job)
    step_info = STEP_LABELS.get(job.status, ("Unknown", 0))
    return jsonify({
        "job_id":     job.job_id, "status": job.status,
        "step_label": step_info[0], "percent": step_info[1],
        "vm_id":      job.vm_id,   "vm_ip":   job.vm_ip,
        "simulated":  job.simulated,
        "has_result": bool(job.result_file or job.result_path),
        "result_file":job.result_file, "error_msg": job.error_msg,
        "elapsed":    round(time.time() - job.created_at),
        "task_type":  job.task_type,
    })


@jobs_bp.route("/results/<job_id>")
@login_required
def job_results(job_id):
    job = Job.query.filter_by(job_id=job_id).first_or_404()
    if session["role"] != "Admin" and job.user_id != session["user_id"]:
        abort(403)
    if job.status != "completed":
        flash("Results are not ready yet.", "warning")
        return redirect(url_for("jobs.job_progress", job_id=job_id))
    summary = {}
    charts  = []
    if job.result_path:
        rj = Path(job.result_path) / "results.json"
        if rj.exists():
            with open(rj) as fh:
                summary = json.load(fh)
        charts = summary.get("charts", [])
        for c in charts:
            if not c["file"].startswith(job_id):
                c["file"] = f"{job_id}/{c['file']}"
    return render_template("jobs/results.html", job=job, summary=summary, charts=charts)


@jobs_bp.route("/download/<job_id>")
@login_required
def download_result(job_id):
    job = Job.query.filter_by(job_id=job_id).first_or_404()
    if session["role"] != "Admin" and job.user_id != session["user_id"]:
        abort(403)
    advance_job_status(job)
    if job.result_path:
        cleaned = Path(job.result_path) / "cleaned_data.csv"
        if cleaned.exists():
            audit("FILE_DOWNLOAD", resource_type="FILE",
                  resource_id=f"{job_id}/cleaned_data.csv")
            return send_from_directory(str(cleaned.parent), "cleaned_data.csv", as_attachment=True)
    if job.result_file:
        audit("FILE_DOWNLOAD", resource_type="FILE", resource_id=job.result_file)
        return send_from_directory(_results_dir(), job.result_file, as_attachment=True)
    flash("Results not ready yet.", "warning")
    return redirect(url_for("jobs.dashboard"))


@jobs_bp.route("/terminate/<job_id>", methods=["POST"])
@login_required
def terminate_vm(job_id):
    job = Job.query.filter_by(job_id=job_id).first_or_404()
    if session["role"] != "Admin" and job.user_id != session["user_id"]:
        return jsonify({"error": "Forbidden"}), 403
    job.status     = "terminated"
    job.updated_at = time.time()
    db.session.commit()
    AuditLog.write(action="JOB_TERMINATE", user_id=session["user_id"],
                   resource_type="JOB", resource_id=job_id,
                   ip=request.remote_addr)
    db.session.commit()
    flash(f"Job #{job_id} terminated.", "info")
    return redirect(url_for("jobs.dashboard"))
