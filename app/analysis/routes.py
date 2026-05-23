"""
analysis/routes.py — Local pandas+ML quick analysis pipeline.
"""
import json
import os
from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, session, url_for, current_app)
from werkzeug.utils import secure_filename
from app.security import login_required

analysis_bp = Blueprint("analysis", __name__, url_prefix="/analysis")


@analysis_bp.route("/upload", methods=["GET","POST"])
@login_required
def upload():
    if request.method == "POST":
        file = request.files.get("dataset")
        if not file or not file.filename:
            flash("Please select a CSV file.", "danger")
            return redirect(url_for("analysis.upload"))
        if not file.filename.lower().endswith(".csv"):
            flash("Only CSV files are supported.", "danger")
            return redirect(url_for("analysis.upload"))

        operations = request.form.getlist("operations")
        if not operations:
            flash("Please select at least one operation.", "danger")
            return redirect(url_for("analysis.upload"))

        import time, uuid
        filename = secure_filename(f"{session['username']}_{int(time.time())}_{file.filename}")
        upload_folder = os.path.join(current_app.root_path, "static", "uploads")
        os.makedirs(upload_folder, exist_ok=True)
        file_path = os.path.join(upload_folder, filename)
        file.save(file_path)

        job_id = str(uuid.uuid4())[:8].upper()
        from tasks import process_dataset
        task = process_dataset.delay(job_id, file_path, operations)
        flash(f"Processing started — Job #{job_id}", "success")
        return redirect(url_for("analysis.progress", task_id=task.id, job_id=job_id))

    return render_template("analysis/upload.html")


@analysis_bp.route("/progress/<task_id>/<job_id>")
@login_required
def progress(task_id, job_id):
    return render_template("analysis/progress.html", task_id=task_id, job_id=job_id)


@analysis_bp.route("/status/<task_id>")
@login_required
def task_status(task_id):
    from celery_app import celery
    task = celery.AsyncResult(task_id)
    if task.state == "PENDING":
        return jsonify({"state":"PENDING","percent":0,"step":"Waiting in queue..."})
    elif task.state == "PROGRESS":
        meta = task.info or {}
        return jsonify({"state":"PROGRESS","percent":meta.get("percent",0),"step":meta.get("step","Processing...")})
    elif task.state == "SUCCESS":
        r = task.result or {}
        return jsonify({"state":"SUCCESS","percent":100,"step":"Done!","results":r.get("results",{}),"job_id":r.get("job_id","")})
    return jsonify({"state":"FAILURE","percent":0,"step":str(task.info)})


@analysis_bp.route("/results/<job_id>")
@login_required
def results(job_id):
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    summary_path = os.path.join(base, "app", "static", "results", job_id, "summary.json")
    if not os.path.exists(summary_path):
        flash("Results not found or still processing.", "warning")
        return redirect(url_for("analysis.upload"))
    with open(summary_path) as f:
        summary = json.load(f)
    return render_template("analysis/results.html", summary=summary, job_id=job_id)
