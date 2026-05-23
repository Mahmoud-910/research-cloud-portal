"""
tasks_octave.py — Celery task بتشغل MATLAB/Octave scripts حقيقية.
الـ user بيرفع .m file والبروجكت بيشغله ويرجعله النتيجة.
"""

import os
import subprocess
import json
import uuid
import traceback
from celery_app import celery

RESULTS_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), "app", "static", "results"))
UPLOAD_FOLDER  = os.path.abspath(os.path.join(os.path.dirname(__file__), "static", "uploads"))
os.makedirs(RESULTS_FOLDER, exist_ok=True)


def update_progress(task, step: str, percent: int):
    task.update_state(state="PROGRESS", meta={"step": step, "percent": percent})


@celery.task(bind=True, name="tasks_octave.run_octave_script")
def run_octave_script(self, job_id: str, script_path: str, csv_path: str = None):
    """
    بتشغل Octave script وترجع النتيجة.

    Parameters:
        job_id      : رقم الـ job
        script_path : المسار للـ .m file
        csv_path    : CSV اختياري — بيتحمل تلقائياً في الـ script كـ 'data'
    """
    result_folder = os.path.join(RESULTS_FOLDER, job_id)
    os.makedirs(result_folder, exist_ok=True)

    # Convert relative paths to absolute
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if script_path and not os.path.isabs(script_path):
        script_path = os.path.join(base_dir, script_path)
    if csv_path and not os.path.isabs(csv_path):
        csv_path = os.path.join(base_dir, csv_path)

    try:
        update_progress(self, "Preparing Octave environment...", 10)

        if csv_path and os.path.exists(csv_path):
            wrapper = _build_wrapper(script_path, csv_path, result_folder)
        else:
            wrapper = _build_wrapper_no_csv(script_path, result_folder)

        wrapper_path = os.path.join(result_folder, "run_script.m")
        with open(wrapper_path, "w") as f:
            f.write(wrapper)

        update_progress(self, "Running Octave script...", 40)

        proc = subprocess.run(
            ["octave", "--no-gui", "--no-window-system", wrapper_path],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=result_folder,
        )

        update_progress(self, "Collecting results...", 80)

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        output_path = os.path.join(result_folder, "octave_output.txt")
        with open(output_path, "w") as f:
            f.write("=== STDOUT ===\n")
            f.write(stdout or "(no output)")
            f.write("\n\n=== STDERR ===\n")
            f.write(stderr or "(none)")

        csv_results = []
        for fname in os.listdir(result_folder):
            if fname.endswith(".csv") and fname != os.path.basename(csv_path or ""):
                csv_results.append(fname)

        success = proc.returncode == 0

        summary = {
            "job_id":       job_id,
            "success":      success,
            "return_code":  proc.returncode,
            "stdout":       stdout[:3000] if stdout else "",
            "stderr":       stderr[:1000] if stderr else "",
            "output_file":  f"{job_id}/octave_output.txt",
            "csv_results":  [f"{job_id}/{f}" for f in csv_results],
            "script_name":  os.path.basename(script_path),
        }

        summary_path = os.path.join(result_folder, "summary.json")
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)

        update_progress(self, "Done!", 100)
        return {"status": "SUCCESS", "job_id": job_id, "results": summary}

    except subprocess.TimeoutExpired:
        error = "Script exceeded 120 seconds timeout."
        _save_error(result_folder, job_id, error)
        raise Exception(error)

    except FileNotFoundError:
        error = "Octave is not installed. Run: sudo apt install octave -y"
        _save_error(result_folder, job_id, error)
        raise Exception(error)

    except Exception as e:
        error = traceback.format_exc()
        _save_error(result_folder, job_id, error)
        raise self.retry(exc=e, max_retries=0)


def _build_wrapper(script_path: str, csv_path: str, result_folder: str) -> str:
    """
    Wrapper بيحمل الـ CSV تلقائياً كـ variable اسمه 'data'
    قبل ما يشغل الـ script بتاع الـ user.
    """
    return f"""
% Auto-generated wrapper by Research Cloud Portal
% يحمل الـ CSV تلقائياً عشان الـ user يستخدمه في الـ script بتاعه

try
  % تحميل الـ CSV
  raw = csvread('{csv_path}', 1, 0);  % skip header row
  data = raw;
  fprintf('✓ Dataset loaded: %d rows x %d cols\\n', rows(data), columns(data));
catch e
  fprintf('Warning: Could not load CSV: %s\\n', e.message);
  data = [];
end

% تغيير الـ directory للـ results folder
cd('{result_folder}');

% تشغيل سكريبت الـ user
fprintf('\\n=== Running: {os.path.basename(script_path)} ===\\n');
source('{script_path}');
fprintf('\\n=== Script finished ===\\n');
"""


def _build_wrapper_no_csv(script_path: str, result_folder: str) -> str:
    return f"""
% Auto-generated wrapper
cd('{result_folder}');
fprintf('=== Running: {os.path.basename(script_path)} ===\\n');
source('{script_path}');
fprintf('=== Script finished ===\\n');
"""


def _save_error(result_folder: str, job_id: str, error: str):
    summary = {"job_id": job_id, "success": False, "error": error}
    with open(os.path.join(result_folder, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
