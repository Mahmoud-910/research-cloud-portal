"""
octave/routes.py — GNU Octave script runner.
"""
import os
from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, session, url_for, current_app)
from werkzeug.utils import secure_filename
from app.security import login_required

octave_bp = Blueprint("octave", __name__, url_prefix="/octave")


@octave_bp.route("/upload", methods=["GET","POST"])
@login_required
def upload():
    if request.method == "POST":
        script  = request.files.get("script")
        dataset = request.files.get("dataset")
        if not script or not script.filename.endswith(".m"):
            flash("Please upload a .m Octave script.", "danger")
            return redirect(url_for("octave.upload"))

        import time, uuid
        upload_folder = os.path.join(current_app.root_path, "static", "uploads")
        os.makedirs(upload_folder, exist_ok=True)

        script_name = secure_filename(f"{session['username']}_{int(time.time())}_{script.filename}")
        script_path = os.path.join(upload_folder, script_name)
        script.save(script_path)

        csv_path = None
        if dataset and dataset.filename and dataset.filename.lower().endswith(".csv"):
            csv_name = secure_filename(f"{session['username']}_{int(time.time())}_{dataset.filename}")
            csv_path = os.path.join(upload_folder, csv_name)
            dataset.save(csv_path)

        job_id = str(uuid.uuid4())[:8].upper()
        from tasks_octave import run_octave_script
        task = run_octave_script.delay(job_id, script_path, csv_path)
        flash(f"Octave job #{job_id} started!", "success")
        return redirect(url_for("octave.progress", task_id=task.id, job_id=job_id))

    return render_template("octave/upload.html")


@octave_bp.route("/progress/<task_id>/<job_id>")
@login_required
def progress(task_id, job_id):
    return render_template("octave/progress.html", task_id=task_id, job_id=job_id)


@octave_bp.route("/status/<task_id>")
@login_required
def task_status(task_id):
    from celery_app import celery
    task = celery.AsyncResult(task_id)
    if task.state == "PENDING":
        return jsonify({"state":"PENDING","percent":0,"step":"Waiting..."})
    elif task.state == "PROGRESS":
        meta = task.info or {}
        return jsonify({"state":"PROGRESS","percent":meta.get("percent",0),"step":meta.get("step","Running...")})
    elif task.state == "SUCCESS":
        r = task.result or {}
        return jsonify({"state":"SUCCESS","percent":100,"step":"Done!","job_id":r.get("job_id",""),"results":r.get("results",{})})
    return jsonify({"state":"FAILURE","percent":0,"step":str(task.info)})


@octave_bp.route("/results/<job_id>")
@login_required
def results(job_id):
    import json
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    summary_path = os.path.join(base, "app", "static", "results", job_id, "summary.json")
    if not os.path.exists(summary_path):
        flash("Results not found.", "warning")
        return redirect(url_for("octave.upload"))
    with open(summary_path) as f:
        summary = json.load(f)
    return render_template("octave/results.html", summary=summary, job_id=job_id)
